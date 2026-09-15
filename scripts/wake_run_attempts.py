"""Execution-attempt aggregation and economical monitor decisions."""

from __future__ import annotations

from typing import Callable

from wake_run_models import ExecutionResult, TriageDecision, WorkerOutcome, WorkerRequest

TRIAGE_RETRY_EXACT = "retry_exact"
TRIAGE_TERMINAL_ACTIONS = frozenset({"report_success", "escalate"})
TriageCallback = Callable[[WorkerRequest, ExecutionResult, int], TriageDecision | None]
StageDelivery = Callable[[object, str], None]
ExecuteCallback = Callable[..., ExecutionResult]


def execute_attempts(
    request: WorkerRequest,
    triage: TriageCallback | None,
    goal_guard: dict[str, object],
    deliver_stage: StageDelivery | None,
    *,
    execute: ExecuteCallback,
) -> WorkerOutcome:
    attempts: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    retry_count = 0
    duration_seconds = 0.0
    user_seconds: float | None = 0.0
    system_seconds: float | None = 0.0
    while True:
        attempt_number = retry_count + 1
        result = execute(
            request,
            attempt_number=attempt_number,
            confirm_startup=attempt_number == 1 and request.startup_file is not None,
            goal_guard=goal_guard,
            deliver_stage=deliver_stage,
        )
        attempts.append(_attempt_record(attempt_number, result))
        duration_seconds += result.duration_seconds or 0.0
        user_seconds = _accumulate_metric(user_seconds, result.user_seconds)
        system_seconds = _accumulate_metric(system_seconds, result.system_seconds)
        outcome = _without_triage(
            result,
            triage,
            attempts,
            decisions,
            duration_seconds,
            user_seconds,
            system_seconds,
        )
        if outcome is not None:
            return outcome
        try:
            decision = triage(request, result, retry_count)
            if decision is None:
                status = "skipped_success" if result.exit_code == 0 and result.error is None else "skipped_failure"
                return _outcome(result, attempts, decisions, status, None, duration_seconds, user_seconds, system_seconds)
            decisions.append(_decision_record(attempt_number, decision))
            if decision.action == TRIAGE_RETRY_EXACT:
                retry_count += 1
                continue
            if decision.action not in TRIAGE_TERMINAL_ACTIONS:
                raise RuntimeError(f"Unknown monitor decision: {decision.action}")
            return _outcome(result, attempts, decisions, "reviewed", None, duration_seconds, user_seconds, system_seconds)
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            return _outcome(result, attempts, decisions, "error", detail, duration_seconds, user_seconds, system_seconds)


def _without_triage(
    result: ExecutionResult,
    triage: TriageCallback | None,
    attempts: list[dict[str, object]],
    decisions: list[dict[str, object]],
    duration: float,
    user: float | None,
    system: float | None,
) -> WorkerOutcome | None:
    if result.terminal_state in {"detached", "cancelled"}:
        return _outcome(
            result, attempts, decisions, f"not_run_{result.terminal_state}", None,
            duration, user, system,
        )
    if result.startup_confirmed and triage is not None:
        return None
    status = "disabled" if triage is None else "not_run"
    return _outcome(result, attempts, decisions, status, None, duration, user, system)


def _outcome(
    result: ExecutionResult,
    attempts: list[dict[str, object]],
    decisions: list[dict[str, object]],
    status: str,
    error: str | None,
    duration: float,
    user: float | None,
    system: float | None,
) -> WorkerOutcome:
    return WorkerOutcome(
        result=result,
        attempts=tuple(attempts),
        triage=tuple(decisions),
        monitor_status=status,
        monitor_error=error,
        duration_seconds=duration,
        user_seconds=user,
        system_seconds=system,
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


def _accumulate_metric(total: float | None, value: float | None) -> float | None:
    if total is None or value is None:
        return None
    return total + value
