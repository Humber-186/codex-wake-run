"""Worker process lifecycle for wake-run."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from wake_run_metrics import collect_metrics, cpu_usage_snapshot
from wake_run_platform import build_experiment_invocation
from wake_run_process import ProcessTreeSignalGuard, target_popen_kwargs, terminate_process_tree
from wake_run_goal import GoalGuardContext, cancel_goal_guard, mark_goal_holder_started
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
TRIAGE_RETRY_EXACT = "retry_exact"
TRIAGE_TERMINAL_ACTIONS = frozenset({"report_success", "escalate"})
GATE_CHECK_INTERVAL = 0.05


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
    gate_file: Path | None = None
    launcher_pid: int | None = None


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int | None
    error: str | None
    startup_confirmed: bool
    duration_seconds: float | None = None
    user_seconds: float | None = None
    system_seconds: float | None = None


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
    duration_seconds: float
    user_seconds: float | None
    system_seconds: float | None


@dataclass(frozen=True)
class StateFailure:
    request: WorkerRequest
    outcome: WorkerOutcome
    wake_id: str
    error: str
    goal_guard: dict[str, object]


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
        if request.gate_file is not None:
            try:
                mark_goal_holder_started(
                    GoalGuardContext(thread_id=request.thread_id, codex_bin=request.codex_bin),
                    run_id=request.run_id,
                )
            except Exception as error:
                return None, _cleanup_failure(process, error), False
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
    started_at = time.monotonic()
    before_cpu = cpu_usage_snapshot()
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
                    return _execution_result(
                        exit_code=None,
                        error=error,
                        startup_confirmed=startup_confirmed,
                        started_at=started_at,
                        before_cpu=before_cpu,
                    )
                assert process is not None
                try:
                    exit_code = process.wait()
                    return _execution_result(
                        exit_code=exit_code,
                        error=None,
                        startup_confirmed=startup_confirmed,
                        started_at=started_at,
                        before_cpu=before_cpu,
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
) -> ExecutionResult:
    metrics = collect_metrics(started_at, before_cpu)
    return ExecutionResult(
        exit_code=exit_code,
        error=error,
        startup_confirmed=startup_confirmed,
        duration_seconds=metrics.wall_seconds,
        user_seconds=metrics.user_seconds,
        system_seconds=metrics.system_seconds,
    )


def _attempt_record(number: int, result: ExecutionResult) -> dict[str, object]:
    return {
        "attempt": number,
        "exit_code": result.exit_code,
        "error": result.error,
        "duration_seconds": result.duration_seconds,
        "user_seconds": result.user_seconds,
        "system_seconds": result.system_seconds,
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
    duration_seconds = 0.0
    user_seconds: float | None = 0.0
    system_seconds: float | None = 0.0
    while True:
        attempt_number = retry_count + 1
        result = _execute(
            request,
            attempt_number=attempt_number,
            confirm_startup=attempt_number == 1 and request.startup_file is not None,
        )
        attempts.append(_attempt_record(attempt_number, result))
        if result.duration_seconds is not None:
            duration_seconds += result.duration_seconds
        user_seconds = _accumulate_metric(user_seconds, result.user_seconds)
        system_seconds = _accumulate_metric(system_seconds, result.system_seconds)
        if not result.startup_confirmed or triage is None:
            return WorkerOutcome(
                result=result,
                attempts=tuple(attempts),
                triage=tuple(decisions),
                monitor_error=None,
                duration_seconds=duration_seconds,
                user_seconds=user_seconds,
                system_seconds=system_seconds,
            )
        try:
            decision = triage(request, result, retry_count)
            decisions.append(_decision_record(attempt_number, decision))
            if decision.action == TRIAGE_RETRY_EXACT:
                retry_count += 1
                continue
            if decision.action not in TRIAGE_TERMINAL_ACTIONS:
                raise RuntimeError(f"Unknown monitor decision: {decision.action}")
            return WorkerOutcome(
                result=result,
                attempts=tuple(attempts),
                triage=tuple(decisions),
                monitor_error=None,
                duration_seconds=duration_seconds,
                user_seconds=user_seconds,
                system_seconds=system_seconds,
            )
        except Exception as error:
            return WorkerOutcome(
                result=result,
                attempts=tuple(attempts),
                triage=tuple(decisions),
                monitor_error=_error_text(error),
                duration_seconds=duration_seconds,
                user_seconds=user_seconds,
                system_seconds=system_seconds,
            )


def _accumulate_metric(total: float | None, value: float | None) -> float | None:
    if total is None or value is None:
        return None
    return total + value


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
            )
        except Exception as error:
            cancellation_failed = True
            detail = f"{detail}; Goal guard cancellation failed: {_error_text(error)}"
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
        monitor_error=outcome.monitor_error,
        goal_guard=goal_guard,
    )


def _await_commit_gate(request: WorkerRequest) -> dict[str, object]:
    if request.startup_file is None and request.gate_file is None:
        return {"mode": "not_needed", "verified": True, "lease_id": None}
    if request.startup_file is None or request.gate_file is None:
        raise RuntimeError("worker startup_file and gate_file must be provided together")
    if request.launcher_pid is None or request.launcher_pid <= 0:
        raise RuntimeError("two-phase worker requires a valid launcher_pid")
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
) -> int:
    try:
        goal_guard = _await_commit_gate(request)
    except Exception as error:
        return _report_gate_failure(request, error)
    outcome = _execute_attempts(request, triage)
    result = outcome.result
    if not result.startup_confirmed:
        return _report_startup_failure(request, result, goal_guard)
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
