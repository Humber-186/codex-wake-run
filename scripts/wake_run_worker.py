"""Worker process lifecycle for wake-run."""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from wake_run_platform import build_experiment_invocation
from wake_run_state import create_completion_event, open_private_log, write_startup_status

WORKER_DELIVERY_FAILURE = 70
WORKER_STATE_FAILURE = 74
WORKER_STARTUP_FAILURE = 127
WORKER_STOP_TIMEOUT = 5


@dataclass(frozen=True)
class WorkerRequest:
    thread_id: str
    command: str
    cwd: Path
    log_file: Path
    codex_bin: str
    run_id: str
    startup_file: Path | None


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int | None
    error: str | None
    startup_confirmed: bool


@dataclass(frozen=True)
class StateFailure:
    request: WorkerRequest
    result: ExecutionResult
    wake_id: str
    error: str


CompletionDelivery = Callable[[Path, str], None]
StateFailureNotifier = Callable[[StateFailure], int]


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _terminate_unarmed_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=WORKER_STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _start_process(
    request: WorkerRequest,
    log: BinaryIO,
) -> tuple[subprocess.Popen[bytes] | None, str | None, bool]:
    try:
        process = subprocess.Popen(
            build_experiment_invocation(request.command),
            cwd=str(request.cwd),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    except Exception as error:
        return None, _error_text(error), request.startup_file is None
    if request.startup_file is None:
        return process, None, True
    try:
        write_startup_status(
            request.startup_file,
            state="running",
            run_id=request.run_id,
            worker_pid=os.getpid(),
            process_pid=process.pid,
        )
    except Exception as error:
        if process.poll() is None:
            _terminate_unarmed_process(process)
        return None, _error_text(error), False
    return process, None, True


def _execute(request: WorkerRequest) -> ExecutionResult:
    try:
        with open_private_log(request.log_file) as log:
            log.write(f"$ {request.command}\n".encode("utf-8", errors="replace"))
            process, error, startup_confirmed = _start_process(request, log)
            if error is not None:
                log.write(f"\n[wake-run] execution failed: {error}\n".encode("utf-8"))
                return ExecutionResult(None, error, startup_confirmed)
            assert process is not None
            try:
                return ExecutionResult(process.wait(), None, startup_confirmed)
            except Exception as wait_error:
                error = _error_text(wait_error)
                log.write(f"\n[wake-run] process wait failed: {error}\n".encode("utf-8"))
                return ExecutionResult(None, error, startup_confirmed)
    except Exception as error:
        return ExecutionResult(None, _error_text(error), request.startup_file is None)


def _report_startup_failure(request: WorkerRequest, result: ExecutionResult) -> int:
    assert request.startup_file is not None
    assert result.error is not None
    write_startup_status(
        request.startup_file,
        state="startup_failed",
        run_id=request.run_id,
        worker_pid=os.getpid(),
        error=result.error,
    )
    return WORKER_STARTUP_FAILURE


def _persist_completion(
    request: WorkerRequest,
    result: ExecutionResult,
    wake_id: str,
) -> Path:
    return create_completion_event(
        request.log_file,
        run_id=request.run_id,
        thread_id=request.thread_id,
        command=request.command,
        exit_code=result.exit_code,
        launch_error=result.error,
        wake_id=wake_id,
    )


def run_worker(
    request: WorkerRequest,
    *,
    deliver: CompletionDelivery,
    notify_state_failure: StateFailureNotifier,
) -> int:
    result = _execute(request)
    if not result.startup_confirmed:
        return _report_startup_failure(request, result)
    wake_id = uuid.uuid4().hex
    try:
        completion_file = _persist_completion(request, result, wake_id)
    except Exception as error:
        state_error = _error_text(error)
        with open_private_log(request.log_file) as log:
            log.write(f"\n[wake-run] completion persistence failed: {state_error}\n".encode("utf-8"))
        return notify_state_failure(StateFailure(request, result, wake_id, state_error))
    try:
        deliver(completion_file, request.codex_bin)
    except Exception as error:
        with open_private_log(request.log_file) as log:
            log.write(f"\n[wake-run] completion delivery failed: {_error_text(error)}\n".encode("utf-8"))
        return WORKER_DELIVERY_FAILURE
    if result.error is not None:
        return WORKER_STARTUP_FAILURE
    return result.exit_code if result.exit_code is not None else 1
