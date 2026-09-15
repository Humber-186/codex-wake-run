"""Launch a recovery observer for an orphaned live target."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from wake_run_core import preflight_codex_queue, startup_timeout_from_environment
from wake_run_goal import GoalGuardContext
from wake_run_goal_adopt import adopt_goal_holder
from wake_run_platform import detached_popen_kwargs
from wake_run_process import process_identity_matches, terminate_process_tree
from wake_run_registry import load_run_record
from wake_run_state import atomic_write_json, process_is_alive, read_json, utc_now

ADOPT_POLL_SECONDS = 0.05
ADOPT_WORKER_STOP_TIMEOUT = 5


def adopt_run(run_id: str, *, thread_id: str, codex_bin: str) -> dict[str, object]:
    spec_file, spec, runtime_file, runtime = load_run_record(run_id)
    _validate_candidate(run_id, spec, runtime, thread_id)
    resolved_codex = preflight_codex_queue(codex_bin)
    log_file = Path(_required_text(spec.get("log_file"), "log_file"))
    startup_file = log_file.with_suffix(".adopt-startup.json")
    gate_file = log_file.with_suffix(".adopt-gate.json")
    worker = subprocess.Popen(
        _worker_arguments(
            spec_file,
            runtime_file=runtime_file,
            startup_file=startup_file,
            gate_file=gate_file,
            codex_bin=resolved_codex,
        ),
        **detached_popen_kwargs(),
    )
    try:
        timeout = startup_timeout_from_environment()
        _wait_for_state(worker, startup_file, "prepared", timeout)
        goal_guard = runtime.get("goal_guard")
        if not isinstance(goal_guard, dict):
            raise RuntimeError("Run runtime has no Goal guard record")
        _adopt_goal_if_needed(spec, runtime, goal_guard, worker.pid, resolved_codex)
        atomic_write_json(gate_file, {
            "state": "committed",
            "run_id": run_id,
            "goal_guard": goal_guard,
            "updated_at": utc_now(),
        })
        running = _wait_for_state(worker, startup_file, "running", timeout)
    except Exception:
        terminate_process_tree(worker, timeout=ADOPT_WORKER_STOP_TIMEOUT)
        raise
    startup_file.unlink()
    gate_file.unlink()
    return {
        "status": "adopted",
        "run_id": run_id,
        "worker_pid": worker.pid,
        "process_pid": running["process_pid"],
        "observer_mode": "adopted",
        "exact_exit_code_available": False,
        "log_file": str(log_file),
    }


def _validate_candidate(
    run_id: str,
    spec: dict[str, object],
    runtime: dict[str, object],
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
    guard: dict[str, object],
    worker_pid: int,
    codex_bin: str,
) -> None:
    lease_id = guard.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        return
    target_pid = runtime.get("target_pid")
    if not isinstance(target_pid, int):
        raise RuntimeError("Run has no target PID for Goal adoption")
    adopt_goal_holder(
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
        "--codex-bin", codex_bin,
    ]


def _wait_for_state(
    worker: subprocess.Popen[bytes],
    path: Path,
    expected: str,
    timeout: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            status = read_json(path)
            if status.get("state") == expected:
                if status.get("worker_pid") != worker.pid:
                    raise RuntimeError("Adopt worker startup has the wrong worker PID")
                return status
        return_code = worker.poll()
        if return_code is not None:
            raise RuntimeError(f"Adopt worker exited before {expected} (exit {return_code})")
        time.sleep(ADOPT_POLL_SECONDS)
    raise RuntimeError(f"Adopt worker did not confirm {expected} within {timeout:g}s")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Run record has no valid {field}")
    return value
