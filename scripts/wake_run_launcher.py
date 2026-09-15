"""Two-phase detached launcher with Goal protection."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from wake_run_core import preflight_codex_queue, startup_timeout_from_environment
from wake_run_goal import (
    GOAL_POLICY_AUTO,
    GoalGuard,
    GoalGuardContext,
    acquire_goal_guard,
    cancel_goal_guard,
)
from wake_run_monitor import (
    MonitorPolicy,
    RuntimeMonitorPlan,
    create_monitor_plan,
    preflight_codex_monitor,
)
from wake_run_platform import detached_popen_kwargs
from wake_run_process import terminate_process_tree
from wake_run_registry import create_run_record, write_run_runtime
from wake_run_state import atomic_write_json, ensure_private_directory, read_json, utc_now, write_startup_status
from wake_run_stages import StageRule, persist_stage_plan

STARTUP_CHECK_INTERVAL = 0.05
WORKER_STOP_TIMEOUT = 5


def _wait_for_state(
    worker: subprocess.Popen[bytes],
    startup_file: Path,
    expected_state: str,
    *,
    timeout: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        if startup_file.exists():
            status = read_json(startup_file)
            state = status.get("state")
            if state == expected_state:
                return status
            if state == "startup_failed":
                raise RuntimeError(f"wake-run worker failed to start the command: {status.get('error')}")
        return_code = worker.poll()
        if return_code is not None:
            raise RuntimeError(f"wake-run worker exited before {expected_state} confirmation (exit {return_code})")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"wake-run worker did not confirm {expected_state} within {timeout:g}s"
            )
        time.sleep(min(STARTUP_CHECK_INTERVAL, remaining))


def _validate_worker_status(
    worker: subprocess.Popen[bytes],
    status: dict[str, object],
    run_id: str,
    *,
    require_process: bool,
) -> None:
    if status.get("run_id") != run_id:
        raise RuntimeError("wake-run startup confirmation has the wrong run_id")
    if status.get("worker_pid") != worker.pid:
        raise RuntimeError("wake-run startup confirmation has the wrong worker_pid")
    process_pid = status.get("process_pid")
    if require_process and (not isinstance(process_pid, int) or process_pid <= 0):
        raise RuntimeError("wake-run startup confirmation has an invalid process_pid")


def _worker_arguments(
    *,
    thread_id: str,
    command: str,
    cwd: Path,
    log_file: Path,
    codex_bin: str,
    run_id: str,
    startup_file: Path,
    gate_file: Path,
    monitor_plan_file: Path | None,
    runtime_file: Path,
    stage_plan_file: Path | None,
) -> list[str]:
    arguments = [
        sys.executable,
        str(Path(__file__).with_name("wake_run.py").resolve()),
        "--worker",
        "--command", command,
        "--thread-id", thread_id,
        "--cwd", str(cwd),
        "--log-file", str(log_file),
        "--codex-bin", codex_bin,
        "--run-id", run_id,
        "--startup-file", str(startup_file),
        "--gate-file", str(gate_file),
        "--launcher-pid", str(os.getpid()),
        "--runtime-file", str(runtime_file),
    ]
    if monitor_plan_file is not None:
        arguments.extend(["--monitor-plan-file", str(monitor_plan_file)])
    if stage_plan_file is not None:
        arguments.extend(["--stage-plan-file", str(stage_plan_file)])
    return arguments


def _cancel_after_failure(
    worker: subprocess.Popen[bytes],
    context: GoalGuardContext,
    run_id: str,
    *,
    error: Exception,
    guard: GoalGuard | None,
) -> None:
    terminate_process_tree(worker, timeout=WORKER_STOP_TIMEOUT)
    try:
        cancel_goal_guard(
            context,
            run_id=run_id,
            lease_id=guard.lease_id if guard is not None else None,
        )
    except Exception as cancel_error:
        raise RuntimeError(
            f"{error}; Goal guard cancellation failed: {type(cancel_error).__name__}: {cancel_error}"
        ) from error


def _launch_worker(
    worker_args: list[str],
    *,
    startup_file: Path,
    runtime_file: Path,
    run_id: str,
) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(worker_args, **detached_popen_kwargs())
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        write_startup_status(
            startup_file, state="startup_failed", run_id=run_id, worker_pid=0, error=error
        )
        write_run_runtime(runtime_file, run_id=run_id, state="startup_failed", error=error)
        raise RuntimeError(f"wake-run worker process could not be created: {error}") from exc


def _armed_result(
    *,
    run_id: str,
    worker: subprocess.Popen[bytes],
    running: dict[str, object],
    log_file: Path,
    guard: GoalGuard,
    monitor_plan: RuntimeMonitorPlan | None,
    cleanup_warnings: list[str],
    stages: tuple[StageRule, ...],
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "armed",
        "run_id": run_id,
        "worker_pid": worker.pid,
        "process_pid": running["process_pid"],
        "log_file": str(log_file),
        "goal_guard": guard.as_dict(),
        "stages": [rule.stage_id for rule in stages],
    }
    if monitor_plan is not None:
        result["monitor"] = {
            "model": monitor_plan.policy.model,
            "session_id": monitor_plan.session_id,
            "session_state": "lazy",
            "review_on": list(monitor_plan.policy.review_on),
            "plan_file": str(monitor_plan.path),
            "policy_hash": monitor_plan.policy_hash,
        }
    if cleanup_warnings:
        result["warning"] = "; ".join(cleanup_warnings)
    if guard.mode == "ignored":
        result["warning"] = (
            str(result.get("warning", "")) + "; Goal protection explicitly disabled"
        ).strip("; ")
    return result


def _commit_launch(
    *,
    worker: subprocess.Popen[bytes],
    startup_file: Path,
    gate_file: Path,
    runtime_file: Path,
    run_id: str,
    thread_id: str,
    codex_bin: str,
    goal_policy: str,
    completion_file: Path,
) -> tuple[GoalGuard, dict[str, object]]:
    context = GoalGuardContext(thread_id=thread_id, codex_bin=codex_bin)
    guard: GoalGuard | None = None
    try:
        timeout = startup_timeout_from_environment()
        prepared = _wait_for_state(worker, startup_file, "prepared", timeout=timeout)
        _validate_worker_status(worker, prepared, run_id, require_process=False)
        guard = acquire_goal_guard(
            context,
            run_id=run_id,
            policy=goal_policy,
            worker_pid=worker.pid,
            completion_file=completion_file,
        )
        atomic_write_json(gate_file, {
            "state": "committed",
            "run_id": run_id,
            "goal_guard": guard.as_dict(),
            "updated_at": utc_now(),
        })
        running = _wait_for_state(worker, startup_file, "running", timeout=timeout)
        _validate_worker_status(worker, running, run_id, require_process=True)
        return guard, running
    except Exception as error:
        _cancel_after_failure(worker, context, run_id, error=error, guard=guard)
        write_run_runtime(
            runtime_file,
            run_id=run_id,
            state="startup_failed",
            worker_pid=worker.pid,
            error=f"{type(error).__name__}: {error}",
        )
        raise


def arm_watcher(
    *,
    thread_id: str,
    command: str,
    cwd: Path,
    log_dir: Path,
    codex_bin: str,
    monitor_policy: MonitorPolicy | None = None,
    goal_policy: str = GOAL_POLICY_AUTO,
    name: str | None = None,
    index_root: Path | None = None,
    stage_plan_source: Path | None = None,
) -> dict[str, object]:
    if not cwd.is_dir():
        raise RuntimeError(f"Working directory does not exist or is not a directory: {cwd}")
    resolved_cwd = cwd.resolve(strict=True)
    resolved_codex = preflight_codex_queue(codex_bin)
    if monitor_policy is not None:
        preflight_codex_monitor(resolved_codex)
    ensure_private_directory(log_dir)
    run_id = uuid.uuid4().hex[:12]
    monitor_plan = None
    if monitor_policy is not None:
        monitor_plan = create_monitor_plan(
            policy=monitor_policy,
            run_id=run_id,
            root_thread_id=thread_id,
            log_dir=log_dir,
        )
    log_file = (log_dir / f"{run_id}.log").resolve()
    stage_plan_file = log_file.with_suffix(".stages.json") if stage_plan_source else None
    stages = (
        persist_stage_plan(stage_plan_source, stage_plan_file)
        if stage_plan_source is not None and stage_plan_file is not None
        else ()
    )
    monitor_record = _monitor_record(monitor_policy, monitor_plan)
    _spec_file, runtime_file = create_run_record(
        run_id=run_id,
        name=name,
        thread_id=thread_id,
        command=command,
        cwd=resolved_cwd,
        log_file=log_file,
        monitor=monitor_record,
        index_root=index_root or log_dir / "index",
        stage_plan_file=stage_plan_file,
    )
    startup_file = log_file.with_suffix(".startup.json")
    gate_file = log_file.with_suffix(".gate.json")
    write_startup_status(startup_file, state="launching", run_id=run_id, worker_pid=0)
    monitor_file = monitor_plan.path if monitor_plan is not None else None
    worker_args = _worker_arguments(
        thread_id=thread_id,
        command=command,
        cwd=resolved_cwd,
        log_file=log_file,
        codex_bin=resolved_codex,
        run_id=run_id,
        startup_file=startup_file,
        gate_file=gate_file,
        monitor_plan_file=monitor_file,
        runtime_file=runtime_file,
        stage_plan_file=stage_plan_file,
    )
    worker = _launch_worker(
        worker_args,
        startup_file=startup_file,
        runtime_file=runtime_file,
        run_id=run_id,
    )
    guard, running = _commit_launch(
        worker=worker,
        startup_file=startup_file,
        gate_file=gate_file,
        runtime_file=runtime_file,
        run_id=run_id,
        thread_id=thread_id,
        codex_bin=resolved_codex,
        goal_policy=goal_policy,
        completion_file=log_file.with_suffix(".completion.json"),
    )
    cleanup_warnings = _cleanup_handshake_files(startup_file, gate_file)
    return _armed_result(
        run_id=run_id,
        worker=worker,
        running=running,
        log_file=log_file,
        guard=guard,
        monitor_plan=monitor_plan,
        cleanup_warnings=cleanup_warnings,
        stages=stages,
    )


def _monitor_record(
    policy: MonitorPolicy | None,
    plan: RuntimeMonitorPlan | None,
) -> dict[str, object]:
    if policy is None:
        return {"enabled": False}
    if plan is None:
        raise RuntimeError("Monitor policy has no persisted runtime plan")
    return {
        "enabled": True,
        "model": policy.model,
        "review_on": list(policy.review_on),
        "plan_file": str(plan.path),
        "runtime_file": str(plan.runtime_path),
        "policy_hash": plan.policy_hash,
    }


def _cleanup_handshake_files(*paths: Path) -> list[str]:
    warnings: list[str] = []
    for path in paths:
        try:
            path.unlink()
        except OSError as error:
            warnings.append(f"Could not remove handshake file {path}: {error}")
    return warnings
