"""Launch a recovery observer for an orphaned live target."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from wake_run_core import preflight_codex_queue, startup_timeout_from_environment
from wake_run_goal import GoalGuardContext
from wake_run_goal_adopt import (
    GoalHolderTransfer,
    adopt_goal_holder,
    rollback_goal_holder_adoption,
)
from wake_run_platform import detached_popen_kwargs
from wake_run_process import process_identity_matches, terminate_process_tree
from wake_run_registry import load_run_record, write_run_runtime
from wake_run_state import atomic_write_json, process_is_alive, process_lock, read_json, utc_now

ADOPT_POLL_SECONDS = 0.05
ADOPT_WORKER_STOP_TIMEOUT = 5


def adopt_run(run_id: str, *, thread_id: str, codex_bin: str) -> dict[str, object]:
    spec_file, spec, runtime_file, runtime = load_run_record(run_id)
    resolved_codex = preflight_codex_queue(codex_bin)
    log_file = Path(_required_text(spec.get("log_file"), "log_file"))
    with process_lock(log_file.with_suffix(".adopt.lock"), "Run adoption"):
        spec_file, spec, runtime_file, runtime = load_run_record(run_id)
        _validate_candidate(run_id, spec, runtime, thread_id=thread_id)
        return _adopt_locked(
            spec_file,
            spec=spec,
            runtime_file=runtime_file,
            runtime=runtime,
            codex_bin=resolved_codex,
        )


def _adopt_locked(
    spec_file: Path,
    *,
    spec: dict[str, object],
    runtime_file: Path,
    runtime: dict[str, object],
    codex_bin: str,
) -> dict[str, object]:
    run_id = str(spec["run_id"])
    log_file = Path(_required_text(spec.get("log_file"), "log_file"))
    attempt_id = uuid.uuid4().hex
    startup_file = log_file.with_name(f"{log_file.stem}.adopt-{attempt_id}.startup.json")
    gate_file = log_file.with_name(f"{log_file.stem}.adopt-{attempt_id}.gate.json")
    worker = subprocess.Popen(
        _worker_arguments(
            spec_file,
            runtime_file=runtime_file,
            startup_file=startup_file,
            gate_file=gate_file,
            codex_bin=codex_bin,
            attempt_id=attempt_id,
        ),
        **detached_popen_kwargs(),
    )
    transfer: GoalHolderTransfer | None = None
    try:
        timeout = startup_timeout_from_environment()
        _wait_for_state(
            worker,
            startup_file,
            "prepared",
            timeout=timeout,
            run_id=run_id,
            attempt_id=attempt_id,
        )
        goal_guard = runtime.get("goal_guard")
        if not isinstance(goal_guard, dict):
            raise RuntimeError("Run runtime has no Goal guard record")
        transfer = _adopt_goal_if_needed(
            spec,
            runtime,
            guard=goal_guard,
            worker_pid=worker.pid,
            codex_bin=codex_bin,
        )
        atomic_write_json(gate_file, {
            "state": "committed",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "goal_guard": goal_guard,
            "updated_at": utc_now(),
        })
        running = _wait_for_state(
            worker,
            startup_file,
            "running",
            timeout=timeout,
            run_id=run_id,
            attempt_id=attempt_id,
        )
    except Exception as error:
        rollback_errors = _rollback_failed_adopt(
            worker,
            spec=spec,
            runtime_file=runtime_file,
            runtime=runtime,
            goal_guard=runtime.get("goal_guard"),
            transfer=transfer,
            codex_bin=codex_bin,
        )
        cleanup_warnings = _cleanup_handshake_files(
            startup_file, gate_file, missing_ok=True
        )
        details = [*rollback_errors, *cleanup_warnings]
        if details:
            raise RuntimeError(f"{error}; {'; '.join(details)}") from error
        raise
    warnings = _cleanup_handshake_files(startup_file, gate_file)
    result: dict[str, object] = {
        "status": "adopted",
        "run_id": run_id,
        "worker_pid": worker.pid,
        "process_pid": running["process_pid"],
        "observer_mode": "adopted",
        "exact_exit_code_available": False,
        "log_file": str(log_file),
    }
    if warnings:
        result["warning"] = "; ".join(warnings)
    return result


def _validate_candidate(
    run_id: str,
    spec: dict[str, object],
    runtime: dict[str, object],
    *,
    thread_id: str,
) -> None:
    if spec.get("owner_thread_id") != thread_id:
        raise RuntimeError(f"Run {run_id} is owned by another Codex thread")
    completion = Path(_required_text(spec.get("completion_file"), "completion_file"))
    if completion.exists():
        raise RuntimeError(f"Run {run_id} already has a terminal event")
    worker_pid = runtime.get("worker_pid")
    if isinstance(worker_pid, int) and process_is_alive(worker_pid):
        raise RuntimeError(f"Run {run_id} worker is still alive")
    if runtime.get("state") != "running":
        raise RuntimeError(f"Run {run_id} cannot be adopted in state {runtime.get('state')}")
    identity = runtime.get("target_identity")
    if not isinstance(identity, dict):
        raise RuntimeError(f"Run {run_id} has no Linux target identity")
    if not process_identity_matches(identity):
        raise RuntimeError(f"Run {run_id} target identity is no longer alive")


def _adopt_goal_if_needed(
    spec: dict[str, object],
    runtime: dict[str, object],
    *,
    guard: dict[str, object],
    worker_pid: int,
    codex_bin: str,
) -> GoalHolderTransfer | None:
    lease_id = guard.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        return None
    target_pid = runtime.get("target_pid")
    if not isinstance(target_pid, int):
        raise RuntimeError("Run has no target PID for Goal adoption")
    return adopt_goal_holder(
        GoalGuardContext(thread_id=str(spec["owner_thread_id"]), codex_bin=codex_bin),
        run_id=str(spec["run_id"]),
        lease_id=lease_id,
        worker_pid=worker_pid,
        target_pid=target_pid,
    )


def _worker_arguments(
    spec_file: Path,
    *,
    runtime_file: Path,
    startup_file: Path,
    gate_file: Path,
    codex_bin: str,
    attempt_id: str,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).with_name("wake_run.py").resolve()),
        "--adopt-worker",
        "--spec-file", str(spec_file),
        "--runtime-file", str(runtime_file),
        "--startup-file", str(startup_file),
        "--gate-file", str(gate_file),
        "--launcher-pid", str(os.getpid()),
        "--attempt-id", attempt_id,
        "--codex-bin", codex_bin,
    ]


def _wait_for_state(
    worker: subprocess.Popen[bytes],
    path: Path,
    expected: str,
    *,
    timeout: float,
    run_id: str,
    attempt_id: str,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            status = read_json(path)
            if status.get("state") == expected:
                if status.get("run_id") != run_id or status.get("attempt_id") != attempt_id:
                    raise RuntimeError("Adopt worker startup identity mismatch")
                if status.get("worker_pid") != worker.pid:
                    raise RuntimeError("Adopt worker startup has the wrong worker PID")
                return status
        return_code = worker.poll()
        if return_code is not None:
            raise RuntimeError(f"Adopt worker exited before {expected} (exit {return_code})")
        time.sleep(ADOPT_POLL_SECONDS)
    raise RuntimeError(f"Adopt worker did not confirm {expected} within {timeout:g}s")


def _rollback_failed_adopt(
    worker: subprocess.Popen[bytes],
    *,
    spec: dict[str, object],
    runtime_file: Path,
    runtime: dict[str, object],
    goal_guard: object,
    transfer: GoalHolderTransfer | None,
    codex_bin: str,
) -> list[str]:
    errors: list[str] = []
    try:
        terminate_process_tree(worker, timeout=ADOPT_WORKER_STOP_TIMEOUT)
    except Exception as error:
        errors.append(f"worker cleanup failed: {type(error).__name__}: {error}")
    if process_is_alive(worker.pid):
        errors.append("adopt worker remains alive; Goal holder transfer was not rolled back")
        return errors
    try:
        _rollback_goal_transfer(
            spec,
            goal_guard,
            transfer=transfer,
            codex_bin=codex_bin,
        )
        write_run_runtime(
            runtime_file,
            run_id=str(spec["run_id"]),
            state=str(runtime.get("state", "running")),
            worker_pid=_required_pid(runtime.get("worker_pid"), "worker_pid"),
            observer_mode=str(runtime.get("observer_mode", "owned")),
        )
    except Exception as error:
        errors.append(f"Goal holder rollback failed: {type(error).__name__}: {error}")
    return errors


def _rollback_goal_transfer(
    spec: dict[str, object],
    goal_guard: object,
    *,
    transfer: GoalHolderTransfer | None,
    codex_bin: str,
) -> None:
    if transfer is None:
        return
    if not isinstance(goal_guard, dict):
        raise RuntimeError("Run runtime has no Goal guard record")
    lease_id = goal_guard.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        raise RuntimeError("Transferred Goal holder has no lease id")
    rollback_goal_holder_adoption(
        GoalGuardContext(thread_id=str(spec["owner_thread_id"]), codex_bin=codex_bin),
        run_id=str(spec["run_id"]),
        lease_id=lease_id,
        transfer=transfer,
    )


def _cleanup_handshake_files(
    *paths: Path,
    missing_ok: bool = False,
) -> list[str]:
    warnings: list[str] = []
    for path in paths:
        try:
            path.unlink(missing_ok=missing_ok)
        except OSError as error:
            warnings.append(f"Could not remove handshake file {path}: {error}")
    return warnings


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Run record has no valid {field}")
    return value


def _required_pid(value: object, field: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"Run record has no valid {field}")
    return value
