"""Worker process lifecycle for wake-run."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import BinaryIO, Callable

from wake_run_metrics import collect_metrics, cpu_usage_snapshot
from wake_run_attempts import execute_attempts
from wake_run_models import (
    ExecutionResult,
    StateFailure,
    TriageDecision,
    WorkerOutcome,
    WorkerRequest,
)
from wake_run_platform import build_experiment_invocation
from wake_run_process import (
    ProcessTreeSignalGuard,
    optional_process_identity,
    target_popen_kwargs,
    terminate_process_tree,
)
from wake_run_registry import write_run_runtime
from wake_run_supervisor import supervise_owned_process
from wake_run_goal import (
    GoalGuardContext,
    cancel_goal_guard,
    mark_goal_holder_running,
    mark_goal_holder_spawning,
)
from wake_run_state import (
    create_completion_event,
    open_private_log,
    process_is_alive,
    read_json,
    write_startup_status,
)

WORKER_DELIVERY_FAILURE = 70
WORKER_STATE_FAILURE = 74
WORKER_MONITOR_FAILURE = 75
WORKER_STARTUP_FAILURE = 127
GATE_CHECK_INTERVAL = 0.05


CompletionDelivery = Callable[[Path, str], None]
StateFailureNotifier = Callable[[StateFailure], int]
TriageCallback = Callable[[WorkerRequest, ExecutionResult, int], TriageDecision | None]
StageDelivery = Callable[[Path, str], None]


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _record_runtime(request: WorkerRequest, state: str, **values: object) -> None:
    if request.runtime_file is None:
        return
    write_run_runtime(
        request.runtime_file,
        run_id=request.run_id,
        state=state,
        **values,
    )


def _paused_lease_id(goal_guard: dict[str, object]) -> str | None:
    if goal_guard.get("mode") != "paused":
        return None
    lease_id = goal_guard.get("lease_id")
    if goal_guard.get("verified") is not True or not isinstance(lease_id, str) or not lease_id:
        raise RuntimeError("paused Goal guard is not verified or has no lease id")
    return lease_id


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
    goal_guard: dict[str, object],
) -> tuple[subprocess.Popen[bytes] | None, str | None, bool]:
    process: subprocess.Popen[bytes] | None = None
    try:
        lease_id = _paused_lease_id(goal_guard)
        if lease_id is not None:
            mark_goal_holder_spawning(
                GoalGuardContext(thread_id=request.thread_id, codex_bin=request.codex_bin),
                run_id=request.run_id,
                lease_id=lease_id,
            )
        process = subprocess.Popen(
            build_experiment_invocation(request.command),
            cwd=str(request.cwd),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            **target_popen_kwargs(),
        )
        signal_guard.attach(process)
        if lease_id is not None:
            mark_goal_holder_running(
                GoalGuardContext(thread_id=request.thread_id, codex_bin=request.codex_bin),
                run_id=request.run_id,
                lease_id=lease_id,
                target_pid=process.pid,
            )
        _record_runtime(
            request, "running", worker_pid=os.getpid(), target_pid=process.pid,
            goal_guard=goal_guard, target_identity=optional_process_identity(process.pid),
            observer_mode="owned",
        )
    except Exception as error:
        if process is not None:
            return None, _cleanup_failure(process, error), False
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
    goal_guard: dict[str, object],
    deliver_stage: StageDelivery | None,
) -> ExecutionResult:
    started_at = time.monotonic()
    before_cpu = cpu_usage_snapshot()
    try:
        with open_private_log(request.log_file) as log:
            log.write(
                f"\n[wake-run] execution attempt {attempt_number}\n$ {request.command}\n".encode(
                    "utf-8", errors="replace"
                )
            )
            initial_log_offset = log.tell()
            with ProcessTreeSignalGuard() as signal_guard:
                process, error, startup_confirmed = _start_process(
                    request,
                    log,
                    signal_guard,
                    confirm_startup=confirm_startup,
                    goal_guard=goal_guard,
                )
                if error is not None:
                    log.write(f"\n[wake-run] execution failed: {error}\n".encode("utf-8"))
                    return _execution_result(
                        exit_code=None,
                        error=error,
                        startup_confirmed=startup_confirmed,
                        started_at=started_at,
                        before_cpu=before_cpu,
                    )
                assert process is not None
                try:
                    if request.stage_plan_file is not None and deliver_stage is None:
                        raise RuntimeError("Stage delivery callback is required for a stage plan")
                    exit_code, terminal_state, stage_error = supervise_owned_process(
                        request,
                        process,
                        initial_log_offset=initial_log_offset,
                        goal_guard=goal_guard,
                        deliver_stage=deliver_stage or _unexpected_stage_delivery,
                        wait_process=_wait_for_process,
                    )
                    return _execution_result(
                        exit_code=exit_code,
                        error=None,
                        startup_confirmed=startup_confirmed,
                        started_at=started_at,
                        before_cpu=before_cpu,
                        terminal_state=terminal_state,
                        stage_delivery_error=stage_error,
                    )
                except Exception as wait_error:
                    error = _cleanup_failure(process, wait_error)
                    log.write(f"\n[wake-run] process wait failed: {error}\n".encode("utf-8"))
                    return _execution_result(
                        exit_code=None,
                        error=error,
                        startup_confirmed=startup_confirmed,
                        started_at=started_at,
                        before_cpu=before_cpu,
                    )
    except Exception as error:
        return _execution_result(
            exit_code=None,
            error=_error_text(error),
            startup_confirmed=not confirm_startup,
            started_at=started_at,
            before_cpu=before_cpu,
        )


def _execution_result(
    *,
    exit_code: int | None,
    error: str | None,
    startup_confirmed: bool,
    started_at: float,
    before_cpu: tuple[float, float] | None,
    terminal_state: str = "completed",
    stage_delivery_error: str | None = None,
) -> ExecutionResult:
    metrics = collect_metrics(started_at, before_cpu)
    return ExecutionResult(
        exit_code=exit_code,
        error=error,
        startup_confirmed=startup_confirmed,
        duration_seconds=metrics.wall_seconds,
        user_seconds=metrics.user_seconds,
        system_seconds=metrics.system_seconds,
        terminal_state=terminal_state,
        stage_delivery_error=stage_delivery_error,
    )


def _unexpected_stage_delivery(_path: Path, _codex_bin: str) -> None:
    raise RuntimeError("Stage delivery callback is unavailable")


def _wait_for_process(process: subprocess.Popen[bytes]) -> int:
    return process.wait()


def _report_startup_failure(
    request: WorkerRequest,
    result: ExecutionResult,
    goal_guard: dict[str, object],
) -> int:
    assert request.startup_file is not None
    assert result.error is not None
    detail = result.error
    cancellation_failed = False
    if goal_guard.get("lease_id"):
        try:
            cancel_goal_guard(
                GoalGuardContext(thread_id=request.thread_id, codex_bin=request.codex_bin),
                run_id=request.run_id,
                lease_id=str(goal_guard["lease_id"]),
            )
        except Exception as error:
            cancellation_failed = True
            detail = f"{detail}; Goal guard cancellation failed: {_error_text(error)}"
    _record_runtime(request, "startup_failed", worker_pid=os.getpid(), error=detail)
    write_startup_status(
        request.startup_file,
        state="startup_failed",
        run_id=request.run_id,
        worker_pid=os.getpid(),
        error=detail,
    )
    return WORKER_STATE_FAILURE if cancellation_failed else WORKER_STARTUP_FAILURE


def _persist_completion(
    request: WorkerRequest,
    outcome: WorkerOutcome,
    wake_id: str,
    goal_guard: dict[str, object],
) -> Path:
    return create_completion_event(
        request.log_file,
        run_id=request.run_id,
        thread_id=request.thread_id,
        command=request.command,
        exit_code=outcome.result.exit_code,
        launch_error=outcome.result.error,
        duration_seconds=outcome.duration_seconds,
        user_seconds=outcome.user_seconds,
        system_seconds=outcome.system_seconds,
        wake_id=wake_id,
        execution_attempts=list(outcome.attempts),
        monitor_plan_file=str(request.monitor_plan_file) if request.monitor_plan_file else None,
        monitor_triage=list(outcome.triage),
        monitor_status=outcome.monitor_status,
        monitor_error=outcome.monitor_error,
        goal_guard=goal_guard,
        terminal_state=outcome.result.terminal_state,
    )


def _await_commit_gate(request: WorkerRequest) -> dict[str, object]:
    if request.startup_file is None and request.gate_file is None:
        return {"mode": "not_needed", "verified": True, "lease_id": None}
    if request.startup_file is None or request.gate_file is None:
        raise RuntimeError("worker startup_file and gate_file must be provided together")
    if request.launcher_pid is None or request.launcher_pid <= 0:
        raise RuntimeError("two-phase worker requires a valid launcher_pid")
    _record_runtime(request, "prepared", worker_pid=os.getpid())
    write_startup_status(
        request.startup_file,
        state="prepared",
        run_id=request.run_id,
        worker_pid=os.getpid(),
    )
    while not request.gate_file.exists():
        if not process_is_alive(request.launcher_pid):
            raise RuntimeError("wake-run launcher exited before committing the launch gate")
        time.sleep(GATE_CHECK_INTERVAL)
    gate = read_json(request.gate_file)
    if gate.get("state") != "committed" or gate.get("run_id") != request.run_id:
        raise RuntimeError("wake-run launch commit gate is invalid")
    goal_guard = gate.get("goal_guard")
    if not isinstance(goal_guard, dict):
        raise RuntimeError("wake-run launch commit gate has no Goal guard")
    return goal_guard


def _report_gate_failure(request: WorkerRequest, error: Exception) -> int:
    detail = _error_text(error)
    cancel_error: Exception | None = None
    try:
        cancel_goal_guard(
            GoalGuardContext(thread_id=request.thread_id, codex_bin=request.codex_bin),
            run_id=request.run_id,
        )
    except Exception as caught:
        cancel_error = caught
        detail = f"{detail}; Goal guard cancellation failed: {_error_text(caught)}"
    assert request.startup_file is not None
    _record_runtime(request, "startup_failed", worker_pid=os.getpid(), error=detail)
    write_startup_status(
        request.startup_file,
        state="startup_failed",
        run_id=request.run_id,
        worker_pid=os.getpid(),
        error=detail,
    )
    if cancel_error is not None:
        return WORKER_STATE_FAILURE
    return WORKER_STARTUP_FAILURE


def run_worker(
    request: WorkerRequest,
    *,
    deliver: CompletionDelivery,
    notify_state_failure: StateFailureNotifier,
    triage: TriageCallback | None = None,
    deliver_stage: StageDelivery | None = None,
) -> int:
    try:
        goal_guard = _await_commit_gate(request)
    except Exception as error:
        return _report_gate_failure(request, error)
    outcome = execute_attempts(
        request, triage, goal_guard, deliver_stage, execute=_execute
    )
    result = outcome.result
    if not result.startup_confirmed:
        return _report_startup_failure(request, result, goal_guard)
    if result.terminal_state == "detached":
        return 0
    wake_id = uuid.uuid4().hex
    try:
        completion_file = _persist_completion(request, outcome, wake_id, goal_guard)
    except Exception as error:
        state_error = _error_text(error)
        with open_private_log(request.log_file) as log:
            log.write(f"\n[wake-run] completion persistence failed: {state_error}\n".encode("utf-8"))
        return notify_state_failure(StateFailure(
            request=request,
            outcome=outcome,
            wake_id=wake_id,
            error=state_error,
            goal_guard=goal_guard,
        ))
    runtime_error: str | None = None
    if request.runtime_file is not None:
        try:
            _record_runtime(
                request,
                result.terminal_state,
                worker_pid=os.getpid(),
                exit_code=result.exit_code,
                error=result.error or outcome.monitor_error,
                completion_file=completion_file,
            )
        except Exception as error:
            runtime_error = _error_text(error)
            with open_private_log(request.log_file) as log:
                log.write(f"\n[wake-run] runtime state update failed: {runtime_error}\n".encode("utf-8"))
    try:
        deliver(completion_file, request.codex_bin)
    except Exception as error:
        with open_private_log(request.log_file) as log:
            log.write(f"\n[wake-run] completion delivery failed: {_error_text(error)}\n".encode("utf-8"))
        return WORKER_DELIVERY_FAILURE
    if result.error is not None:
        return WORKER_STARTUP_FAILURE
    if result.stage_delivery_error is not None:
        return WORKER_DELIVERY_FAILURE
    if outcome.monitor_error is not None:
        return WORKER_MONITOR_FAILURE
    if runtime_error is not None:
        return WORKER_STATE_FAILURE
    return result.exit_code if result.exit_code is not None else 1
