"""Reliable detached execution and wake-up delivery for wake-run."""

from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from wake_run_platform import build_codex_invocation, resolve_codex_executable
from wake_run_messages import build_stage_message, build_wake_message
from wake_run_goal import (
    LEASE_PHASE_RESTORED,
    GoalGuardContext,
    recover_abandoned_goal_leases,
    release_goal_guard,
)
from wake_run_state import (
    DELIVERY_DELIVERED,
    DELIVERY_IN_PROGRESS,
    DELIVERY_PENDING,
    GOAL_RELEASE_NOT_NEEDED,
    GOAL_RELEASE_PENDING,
    GOAL_RELEASE_RESTORED,
    GOAL_RELEASE_RETRYING,
    GOAL_RELEASE_SKIPPED,
    DeliveryInProgressError,
    completion_files,
    completion_goal_release,
    completion_is_undelivered,
    delivery_lock,
    ensure_private_directory,
    open_private_log,
    read_json,
    update_delivery,
    update_goal_release,
    write_startup_status,
)
from wake_run_worker import (
    WORKER_DELIVERY_FAILURE,
    WORKER_MONITOR_FAILURE,
    WORKER_STARTUP_FAILURE,
    WORKER_STATE_FAILURE,
    StateFailure,
    WorkerRequest,
    run_worker as execute_worker,
)
from wake_run_monitor import triage_execution
from wake_run_events import event_is_delivered, stage_event_files

QUEUE_CALL_TIMEOUT, QUEUE_TOTAL_TIMEOUT = 30, 1800
QUEUE_RETRY_DELAYS = (1, 2, 5, 10, 30, 60)
PREFLIGHT_TIMEOUT, STARTUP_WAIT_TIMEOUT = 30, 10

AttemptHook = Callable[[int], None]
FailureHook = Callable[[int, str], None]


@dataclass(frozen=True)
class QueuePolicy:
    call_timeout: float
    total_timeout: float
    retry_delays: tuple[float, ...]

    @classmethod
    def from_environment(cls) -> "QueuePolicy":
        total = float(os.environ.get("WAKE_RUN_QUEUE_TIMEOUT", QUEUE_TOTAL_TIMEOUT))
        if not math.isfinite(total) or total <= 0:
            raise RuntimeError("WAKE_RUN_QUEUE_TIMEOUT must be a finite number greater than zero")
        return cls(QUEUE_CALL_TIMEOUT, total, QUEUE_RETRY_DELAYS)


def startup_timeout_from_environment() -> float:
    timeout = float(os.environ.get("WAKE_RUN_STARTUP_TIMEOUT", STARTUP_WAIT_TIMEOUT))
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("WAKE_RUN_STARTUP_TIMEOUT must be a finite number greater than zero")
    return timeout


def _validate_queue_policy(policy: QueuePolicy) -> None:
    scalars = (policy.call_timeout, policy.total_timeout, *policy.retry_delays)
    if not policy.retry_delays:
        raise RuntimeError("Queue retry delays must not be empty")
    if not all(math.isfinite(value) for value in scalars):
        raise RuntimeError("Queue policy values must be finite")
    if policy.call_timeout <= 0 or policy.total_timeout <= 0:
        raise RuntimeError("Queue timeouts must be greater than zero")
    if any(delay < 0 for delay in policy.retry_delays):
        raise RuntimeError("Queue retry delays must not be negative")


def preflight_codex_queue(codex_bin: str) -> str:
    resolved = resolve_codex_executable(codex_bin)
    try:
        result = subprocess.run(
            build_codex_invocation(resolved, ["queue", "--help"]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=PREFLIGHT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Codex queue preflight timed out after {PREFLIGHT_TIMEOUT}s") from exc
    if result.returncode != 0:
        raise RuntimeError("This Codex CLI does not support `codex queue`.")
    return resolved


def _attempt_queue(thread_id: str, message: str, resolved_codex: str, *, timeout: float) -> None:
    try:
        result = subprocess.run(
            build_codex_invocation(resolved_codex, ["queue", "--thread", thread_id, "--message", message]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"codex queue timed out after {timeout:.3f}s") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"codex queue failed (exit {result.returncode}): {detail}")


def queue_wakeup(
    thread_id: str,
    message: str,
    codex_bin: str,
    *,
    policy: QueuePolicy | None = None,
    before_attempt: AttemptHook | None = None,
    after_failure: FailureHook | None = None,
) -> int:
    active_policy = policy or QueuePolicy.from_environment()
    _validate_queue_policy(active_policy)
    resolved = resolve_codex_executable(codex_bin)
    deadline = time.monotonic() + active_policy.total_timeout
    attempt = 0
    last_error = "no delivery attempt was made"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempt += 1
        if before_attempt:
            before_attempt(attempt)
        try:
            _attempt_queue(
                thread_id,
                message,
                resolved,
                timeout=min(active_policy.call_timeout, max(remaining, 0.001)),
            )
            return attempt
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if after_failure:
                after_failure(attempt, last_error)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        delay = active_policy.retry_delays[min(attempt - 1, len(active_policy.retry_delays) - 1)]
        time.sleep(min(delay, remaining))
    raise RuntimeError(
        f"codex queue failed after {attempt} attempt(s) over "
        f"{active_policy.total_timeout:g}s. Last error: {last_error}"
    )


def _event_message(event: dict[str, object]) -> str:
    if event.get("event_type") == "stage":
        return build_stage_message(event)
    return build_wake_message(
        str(event["command"]),
        event.get("exit_code") if isinstance(event.get("exit_code"), int) else None,
        Path(str(event["log_file"])),
        duration_seconds=(
            float(event["duration_seconds"])
            if isinstance(event.get("duration_seconds"), (int, float))
            else None
        ),
        user_seconds=(
            float(event["user_seconds"])
            if isinstance(event.get("user_seconds"), (int, float))
            else None
        ),
        system_seconds=(
            float(event["system_seconds"])
            if isinstance(event.get("system_seconds"), (int, float))
            else None
        ),
        run_id=str(event["run_id"]),
        wake_id=str(event["wake_id"]),
        monitor=event.get("monitor") if isinstance(event.get("monitor"), dict) else None,
        terminal_state=str(event.get("terminal_state", "completed")),
        observer_mode=str(event.get("observer_mode", "owned")),
    )


def deliver_completion(completion_file: Path, codex_bin: str) -> None:
    with delivery_lock(completion_file):
        event = read_json(completion_file)
        delivery = event.get("delivery")
        if not isinstance(delivery, dict):
            raise RuntimeError(f"Missing delivery state in {completion_file}")
        if delivery.get("state") != DELIVERY_DELIVERED:
            _deliver_wake_event(completion_file, codex_bin, event, delivery)
            event = read_json(completion_file)
        _release_event_goal(completion_file, codex_bin, event)


def deliver_stage_event(event_file: Path, codex_bin: str) -> None:
    with delivery_lock(event_file):
        event = read_json(event_file)
        if event.get("event_type") != "stage":
            raise RuntimeError(f"Not a stage event: {event_file}")
        delivery = event.get("delivery")
        if not isinstance(delivery, dict):
            raise RuntimeError(f"Missing delivery state in {event_file}")
        if delivery.get("state") != DELIVERY_DELIVERED:
            _deliver_wake_event(event_file, codex_bin, event, delivery)


def _deliver_wake_event(
    completion_file: Path,
    codex_bin: str,
    event: dict[str, object],
    delivery: dict[str, object],
) -> None:
    prior_attempts = delivery.get("attempts")
    if not isinstance(prior_attempts, int) or prior_attempts < 0:
        raise RuntimeError(f"Invalid delivery attempt count in {completion_file}")

    def before_attempt(attempt: int) -> None:
        update_delivery(
            completion_file,
            state=DELIVERY_IN_PROGRESS,
            attempts=prior_attempts + attempt,
            last_error=None,
        )

    def after_failure(attempt: int, error: str) -> None:
        update_delivery(
            completion_file,
            state=DELIVERY_PENDING,
            attempts=prior_attempts + attempt,
            last_error=error,
        )

    attempts = queue_wakeup(
        str(event["thread_id"]),
        _event_message(event),
        codex_bin,
        before_attempt=before_attempt,
        after_failure=after_failure,
    )
    update_delivery(
        completion_file,
        state=DELIVERY_DELIVERED,
        attempts=prior_attempts + attempts,
        last_error=None,
    )


def _release_event_goal(
    completion_file: Path,
    codex_bin: str,
    event: dict[str, object],
) -> None:
    release = completion_goal_release(event, completion_file)
    state = release.get("state")
    if state in {GOAL_RELEASE_NOT_NEEDED, GOAL_RELEASE_RESTORED, GOAL_RELEASE_SKIPPED}:
        return
    if state not in {GOAL_RELEASE_PENDING, GOAL_RELEASE_RETRYING}:
        raise RuntimeError(f"Invalid Goal release state in {completion_file}: {state}")
    attempts = release.get("attempts")
    if not isinstance(attempts, int) or attempts < 0:
        raise RuntimeError(f"Invalid Goal release attempt count in {completion_file}")
    guard = event.get("goal_guard")
    lease_id = guard.get("lease_id") if isinstance(guard, dict) else None
    if not isinstance(lease_id, str) or not lease_id:
        raise RuntimeError(f"Pending Goal release has no lease id in {completion_file}")
    update_goal_release(
        completion_file,
        state=GOAL_RELEASE_RETRYING,
        attempts=attempts + 1,
        last_error=None,
    )
    try:
        outcome = release_goal_guard(
            GoalGuardContext(thread_id=str(event["thread_id"]), codex_bin=codex_bin),
            lease_id=lease_id,
        )
    except Exception as error:
        update_goal_release(
            completion_file,
            state=GOAL_RELEASE_RETRYING,
            attempts=attempts + 1,
            last_error=f"{type(error).__name__}: {error}",
        )
        raise
    final_state = (
        GOAL_RELEASE_RESTORED if outcome == LEASE_PHASE_RESTORED else GOAL_RELEASE_SKIPPED
    )
    update_goal_release(
        completion_file,
        state=final_state,
        attempts=attempts + 1,
        last_error=None,
    )


def _notify_state_failure(failure: StateFailure) -> int:
    request = failure.request
    outcome = failure.outcome
    message = build_wake_message(
        request.command,
        outcome.result.exit_code,
        request.log_file,
        duration_seconds=outcome.duration_seconds,
        user_seconds=outcome.user_seconds,
        system_seconds=outcome.system_seconds,
        run_id=request.run_id,
        wake_id=failure.wake_id,
    )
    try:
        queue_wakeup(request.thread_id, message, request.codex_bin)
        lease_id = failure.goal_guard.get("lease_id")
        if isinstance(lease_id, str) and lease_id:
            release_goal_guard(
                GoalGuardContext(thread_id=request.thread_id, codex_bin=request.codex_bin),
                lease_id=lease_id,
            )
    except Exception as delivery_error:
        with open_private_log(request.log_file) as log:
            log.write(
                f"[wake-run] state failure notification also failed: {delivery_error}\n"
                .encode("utf-8", errors="replace")
            )
        return WORKER_DELIVERY_FAILURE
    return WORKER_STATE_FAILURE


def run_worker(
    *,
    thread_id: str,
    command: str,
    cwd: Path,
    log_file: Path,
    codex_bin: str,
    run_id: str = "",
    startup_file: Path | None = None,
    monitor_plan_file: Path | None = None,
    gate_file: Path | None = None,
    launcher_pid: int | None = None,
    runtime_file: Path | None = None,
    stage_plan_file: Path | None = None,
) -> int:
    request = WorkerRequest(
        thread_id=thread_id,
        command=command,
        cwd=cwd,
        log_file=log_file,
        codex_bin=codex_bin,
        run_id=run_id,
        startup_file=startup_file,
        monitor_plan_file=monitor_plan_file,
        gate_file=gate_file,
        launcher_pid=launcher_pid,
        runtime_file=runtime_file,
        stage_plan_file=stage_plan_file,
    )
    return execute_worker(
        request,
        deliver=deliver_completion,
        notify_state_failure=_notify_state_failure,
        triage=triage_execution if monitor_plan_file else None,
        deliver_stage=deliver_stage_event,
    )


def replay_pending(*, log_dir: Path, codex_bin: str) -> dict[str, object]:
    ensure_private_directory(log_dir)
    resolved_codex = preflight_codex_queue(codex_bin)
    delivered: list[str] = []
    busy: list[str] = []
    failures: dict[str, str] = {}
    event_files = [*stage_event_files(log_dir), *completion_files(log_dir)]
    for completion_file in event_files:
        try:
            if completion_file.name.endswith(".event.json"):
                if event_is_delivered(completion_file):
                    continue
                deliver_stage_event(completion_file, resolved_codex)
            else:
                if not completion_is_undelivered(completion_file):
                    continue
                deliver_completion(completion_file, resolved_codex)
            delivered.append(completion_file.name)
        except DeliveryInProgressError as exc:
            busy.append(str(exc))
        except Exception as exc:
            failures[completion_file.name] = f"{type(exc).__name__}: {exc}"
    lease_recovery = recover_abandoned_goal_leases(
        log_dir=log_dir,
        codex_bin=resolved_codex,
    )
    for name, error in lease_recovery["failures"].items():
        failures[f"goal-lease:{name}"] = str(error)
    for name in lease_recovery["orphaned"]:
        failures[f"goal-lease:{name}"] = (
            "Worker exited after target start without a completion event; Goal remains paused"
        )
    incomplete = bool(busy or failures)
    return {
        "status": "replay_incomplete" if incomplete else "replay_complete",
        "delivered": len(delivered),
        "busy": len(busy),
        "failed": len(failures),
        "delivered_files": delivered,
        "busy_events": busy,
        "failures": failures,
        "goal_leases_restored": len(lease_recovery["restored"]),
        "goal_leases_retained": len(lease_recovery["retained"]),
        "goal_leases_orphaned": len(lease_recovery["orphaned"]),
    }
