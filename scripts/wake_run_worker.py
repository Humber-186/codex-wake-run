"""Worker process lifecycle for wake-run."""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from wake_run_platform import build_experiment_invocation
from wake_run_process import ProcessTreeSignalGuard, target_popen_kwargs, terminate_process_tree
from wake_run_state import create_completion_event, open_private_log, write_startup_status

WORKER_DELIVERY_FAILURE = 70
WORKER_STATE_FAILURE = 74
WORKER_MONITOR_FAILURE = 75
WORKER_STARTUP_FAILURE = 127
TRIAGE_RETRY_EXACT = "retry_exact"
TRIAGE_TERMINAL_ACTIONS = frozenset({"report_success", "escalate"})


@dataclass(frozen=True)
class WorkerRequest:
    thread_id: str
    command: str
    cwd: Path
    log_file: Path
    codex_bin: str
    run_id: str
    startup_file: Path | None
    monitor_plan_file: Path | None = None


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


@dataclass(frozen=True)
class TriageDecision:
    action: str
    summary: str
    reason: str
    failure_category: str
    model: str
    session_id: str
    policy_hash: str


@dataclass(frozen=True)
class WorkerOutcome:
    result: ExecutionResult
    attempts: tuple[dict[str, object], ...]
    triage: tuple[dict[str, object], ...]
    monitor_error: str | None


CompletionDelivery = Callable[[Path, str], None]
StateFailureNotifier = Callable[[StateFailure], int]
TriageCallback = Callable[[WorkerRequest, ExecutionResult, int], TriageDecision]


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _cleanup_failure(process: subprocess.Popen[bytes], error: Exception) -> str:
    primary = _error_text(error)
    try:
        terminate_process_tree(process)
    except Exception as cleanup_error:
        return f"{primary}; process-tree cleanup failed: {_error_text(cleanup_error)}"
    return primary


def _start_process(
    request: WorkerRequest,
    log: BinaryIO,
    signal_guard: ProcessTreeSignalGuard,
    *,
    confirm_startup: bool,
) -> tuple[subprocess.Popen[bytes] | None, str | None, bool]:
    try:
        process = subprocess.Popen(
            build_experiment_invocation(request.command),
            cwd=str(request.cwd),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            **target_popen_kwargs(),
        )
        signal_guard.attach(process)
    except Exception as error:
        return None, _error_text(error), not confirm_startup
    if not confirm_startup or request.startup_file is None:
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
            return None, _cleanup_failure(process, error), False
        return None, _error_text(error), False
    return process, None, True


def _execute(
    request: WorkerRequest,
    *,
    attempt_number: int,
    confirm_startup: bool,
) -> ExecutionResult:
    try:
        with open_private_log(request.log_file) as log:
            log.write(
                f"\n[wake-run] execution attempt {attempt_number}\n$ {request.command}\n".encode(
                    "utf-8", errors="replace"
                )
            )
            with ProcessTreeSignalGuard() as signal_guard:
                process, error, startup_confirmed = _start_process(
                    request,
                    log,
                    signal_guard,
                    confirm_startup=confirm_startup,
                )
                if error is not None:
                    log.write(f"\n[wake-run] execution failed: {error}\n".encode("utf-8"))
                    return ExecutionResult(None, error, startup_confirmed)
                assert process is not None
                try:
                    return ExecutionResult(process.wait(), None, startup_confirmed)
                except Exception as wait_error:
                    error = _cleanup_failure(process, wait_error)
                    log.write(f"\n[wake-run] process wait failed: {error}\n".encode("utf-8"))
                    return ExecutionResult(None, error, startup_confirmed)
    except Exception as error:
        return ExecutionResult(None, _error_text(error), not confirm_startup)


def _attempt_record(number: int, result: ExecutionResult) -> dict[str, object]:
    return {
        "attempt": number,
        "exit_code": result.exit_code,
        "error": result.error,
    }


def _decision_record(attempt: int, decision: TriageDecision) -> dict[str, object]:
    return {
        "attempt": attempt,
        "action": decision.action,
        "summary": decision.summary,
        "reason": decision.reason,
        "failure_category": decision.failure_category,
        "model": decision.model,
        "session_id": decision.session_id,
        "policy_hash": decision.policy_hash,
    }


def _execute_attempts(
    request: WorkerRequest,
    triage: TriageCallback | None,
) -> WorkerOutcome:
    attempts: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    retry_count = 0
    while True:
        attempt_number = retry_count + 1
        result = _execute(
            request,
            attempt_number=attempt_number,
            confirm_startup=attempt_number == 1 and request.startup_file is not None,
        )
        attempts.append(_attempt_record(attempt_number, result))
        if not result.startup_confirmed or triage is None:
            return WorkerOutcome(result, tuple(attempts), tuple(decisions), None)
        try:
            decision = triage(request, result, retry_count)
            decisions.append(_decision_record(attempt_number, decision))
            if decision.action == TRIAGE_RETRY_EXACT:
                retry_count += 1
                continue
            if decision.action not in TRIAGE_TERMINAL_ACTIONS:
                raise RuntimeError(f"Unknown monitor decision: {decision.action}")
            return WorkerOutcome(result, tuple(attempts), tuple(decisions), None)
        except Exception as error:
            return WorkerOutcome(result, tuple(attempts), tuple(decisions), _error_text(error))


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
    outcome: WorkerOutcome,
    wake_id: str,
) -> Path:
    return create_completion_event(
        request.log_file,
        run_id=request.run_id,
        thread_id=request.thread_id,
        command=request.command,
        exit_code=outcome.result.exit_code,
        launch_error=outcome.result.error,
        wake_id=wake_id,
        execution_attempts=list(outcome.attempts),
        monitor_plan_file=str(request.monitor_plan_file) if request.monitor_plan_file else None,
        monitor_triage=list(outcome.triage),
        monitor_error=outcome.monitor_error,
    )


def run_worker(
    request: WorkerRequest,
    *,
    deliver: CompletionDelivery,
    notify_state_failure: StateFailureNotifier,
    triage: TriageCallback | None = None,
) -> int:
    outcome = _execute_attempts(request, triage)
    result = outcome.result
    if not result.startup_confirmed:
        return _report_startup_failure(request, result)
    wake_id = uuid.uuid4().hex
    try:
        completion_file = _persist_completion(request, outcome, wake_id)
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
    if outcome.monitor_error is not None:
        return WORKER_MONITOR_FAILURE
    return result.exit_code if result.exit_code is not None else 1
