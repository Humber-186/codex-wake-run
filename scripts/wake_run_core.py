"""Reliable detached execution and wake-up delivery for wake-run."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from wake_run_platform import build_codex_invocation, detached_popen_kwargs, resolve_codex_executable
from wake_run_state import (
    DELIVERY_DELIVERED,
    DELIVERY_IN_PROGRESS,
    DELIVERY_PENDING,
    DeliveryInProgressError,
    completion_files,
    completion_is_undelivered,
    delivery_lock,
    ensure_private_directory,
    open_private_log,
    read_json,
    update_delivery,
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
from wake_run_monitor import MonitorPolicy, create_monitor_session, triage_execution
from wake_run_process import terminate_process_tree

WAKE_HEADER = "[后台任务完成-系统提示]"
QUEUE_CALL_TIMEOUT, QUEUE_TOTAL_TIMEOUT = 30, 1800
QUEUE_RETRY_DELAYS = (1, 2, 5, 10, 30, 60)
PREFLIGHT_TIMEOUT, STARTUP_WAIT_TIMEOUT = 30, 10
STARTUP_CHECK_INTERVAL, WORKER_STOP_TIMEOUT = 0.05, 5

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


def build_wake_message(
    command: str,
    exit_code: int | None,
    log_file: Path,
    *,
    duration_seconds: float | None = None,
    user_seconds: float | None = None,
    system_seconds: float | None = None,
    run_id: str = "",
    wake_id: str = "",
) -> str:
    lines = [
        WAKE_HEADER,
        f"任务：{command}",
        f"日志：{log_file}",
        f"exit_code: {exit_code}",
    ]
    lines.extend(
        f"{label}：{_format_duration(value)}"
        for label, value in (
            ("wall", duration_seconds),
            ("user", user_seconds),
            ("sys", system_seconds),
        )
        if value is not None
    )
    lines.extend([
        f"run_id：{run_id}",
        f"wake_id：{wake_id}",
    ])
    return "\n".join(lines)


def _format_duration(duration_seconds: float) -> str:
    return f"{duration_seconds:.3f}s"


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
    )


def deliver_completion(completion_file: Path, codex_bin: str) -> None:
    with delivery_lock(completion_file):
        event = read_json(completion_file)
        delivery = event.get("delivery")
        if not isinstance(delivery, dict):
            raise RuntimeError(f"Missing delivery state in {completion_file}")
        if delivery.get("state") == DELIVERY_DELIVERED:
            return
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
    )
    return execute_worker(
        request,
        deliver=deliver_completion,
        notify_state_failure=_notify_state_failure,
        triage=triage_execution if monitor_plan_file else None,
    )


def _wait_for_startup(
    worker: subprocess.Popen[bytes],
    startup_file: Path,
    *,
    timeout: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        if startup_file.exists():
            status = read_json(startup_file)
            state = status.get("state")
            if state == "running":
                return status
            if state == "startup_failed":
                raise RuntimeError(f"wake-run worker failed to start the command: {status.get('error')}")
        return_code = worker.poll()
        if return_code is not None:
            raise RuntimeError(f"wake-run worker exited before startup confirmation (exit {return_code})")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"wake-run worker did not confirm startup within {timeout:g}s")
        time.sleep(min(STARTUP_CHECK_INTERVAL, remaining))


def _stop_worker(worker: subprocess.Popen[bytes]) -> None:
    terminate_process_tree(worker, timeout=WORKER_STOP_TIMEOUT)


def _validate_startup_status(
    worker: subprocess.Popen[bytes],
    status: dict[str, object],
    run_id: str,
) -> None:
    if status.get("run_id") != run_id:
        raise RuntimeError("wake-run startup confirmation has the wrong run_id")
    if status.get("worker_pid") != worker.pid:
        raise RuntimeError("wake-run startup confirmation has the wrong worker_pid")
    process_pid = status.get("process_pid")
    if not isinstance(process_pid, int) or process_pid <= 0:
        raise RuntimeError("wake-run startup confirmation has an invalid process_pid")


def arm_watcher(
    *,
    thread_id: str,
    command: str,
    cwd: Path,
    log_dir: Path,
    codex_bin: str,
    monitor_policy: MonitorPolicy | None = None,
) -> dict[str, object]:
    if not cwd.is_dir():
        raise RuntimeError(f"Working directory does not exist or is not a directory: {cwd}")
    resolved_cwd = cwd.resolve(strict=True)
    resolved_codex = preflight_codex_queue(codex_bin)
    ensure_private_directory(log_dir)
    run_id = uuid.uuid4().hex[:12]
    monitor_plan = None
    if monitor_policy is not None:
        monitor_plan = create_monitor_session(
            policy=monitor_policy,
            run_id=run_id,
            root_thread_id=thread_id,
            cwd=resolved_cwd,
            log_dir=log_dir,
            resolved_codex=resolved_codex,
        )
    log_file = (log_dir / f"{run_id}.log").resolve()
    startup_file = log_file.with_suffix(".startup.json")
    write_startup_status(
        startup_file,
        state="launching",
        run_id=run_id,
        worker_pid=0,
    )
    worker_args = [
        sys.executable,
        str(Path(__file__).with_name("wake_run.py").resolve()),
        "--worker",
        "--command", command,
        "--thread-id", thread_id,
        "--cwd", str(resolved_cwd),
        "--log-file", str(log_file),
        "--codex-bin", resolved_codex,
        "--run-id", run_id,
        "--startup-file", str(startup_file),
    ]
    if monitor_plan is not None:
        worker_args.extend(["--monitor-plan-file", str(monitor_plan.path)])
    try:
        worker = subprocess.Popen(worker_args, **detached_popen_kwargs())
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        write_startup_status(
            startup_file,
            state="startup_failed",
            run_id=run_id,
            worker_pid=0,
            error=error,
        )
        raise RuntimeError(f"wake-run worker process could not be created: {error}") from exc
    try:
        status = _wait_for_startup(worker, startup_file, timeout=startup_timeout_from_environment())
        _validate_startup_status(worker, status, run_id)
    except Exception:
        _stop_worker(worker)
        raise
    cleanup_warning: str | None = None
    try:
        startup_file.unlink()
    except OSError as exc:
        cleanup_warning = f"Could not remove startup file {startup_file}: {exc}"
    result: dict[str, object] = {
        "status": "armed",
        "run_id": run_id,
        "worker_pid": worker.pid,
        "process_pid": status["process_pid"],
        "log_file": str(log_file),
    }
    if monitor_plan is not None:
        result["monitor"] = {
            "model": monitor_plan.policy.model,
            "session_id": monitor_plan.session_id,
            "plan_file": str(monitor_plan.path),
            "policy_hash": monitor_plan.policy_hash,
        }
    if cleanup_warning:
        result["warning"] = cleanup_warning
    return result


def replay_pending(*, log_dir: Path, codex_bin: str) -> dict[str, object]:
    ensure_private_directory(log_dir)
    resolved_codex = preflight_codex_queue(codex_bin)
    delivered: list[str] = []
    busy: list[str] = []
    failures: dict[str, str] = {}
    for completion_file in completion_files(log_dir):
        try:
            if not completion_is_undelivered(completion_file):
                continue
            deliver_completion(completion_file, resolved_codex)
            delivered.append(completion_file.name)
        except DeliveryInProgressError as exc:
            busy.append(str(exc))
        except Exception as exc:
            failures[completion_file.name] = f"{type(exc).__name__}: {exc}"
    incomplete = bool(busy or failures)
    return {
        "status": "replay_incomplete" if incomplete else "replay_complete",
        "delivered": len(delivered),
        "busy": len(busy),
        "failed": len(failures),
        "delivered_files": delivered,
        "busy_events": busy,
        "failures": failures,
    }
