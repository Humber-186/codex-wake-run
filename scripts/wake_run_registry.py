"""Persistent run discovery without a central daemon."""

from __future__ import annotations

import datetime
import os
from pathlib import Path

from wake_run_state import atomic_write_json, process_is_alive, read_json, utc_now

RUN_SCHEMA_VERSION = 1
ACTIVE_STATES = frozenset({"launching", "prepared", "running", "orphaned", "lost"})


def run_index_root() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return codex_home.expanduser().resolve() / "wake-run" / "index"


def run_state_paths(log_file: Path) -> tuple[Path, Path]:
    return log_file.with_suffix(".spec.json"), log_file.with_suffix(".runtime.json")


def create_run_record(
    *,
    run_id: str,
    name: str | None,
    thread_id: str,
    command: str,
    cwd: Path,
    log_file: Path,
    monitor: dict[str, object],
    index_root: Path,
    stage_plan_file: Path | None = None,
) -> tuple[Path, Path]:
    spec_file, runtime_file = run_state_paths(log_file)
    created_at = utc_now()
    completion_file = log_file.with_suffix(".completion.json")
    atomic_write_json(spec_file, {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "name": name,
        "owner_thread_id": thread_id,
        "command": command,
        "cwd": str(cwd),
        "log_file": str(log_file),
        "runtime_file": str(runtime_file),
        "completion_file": str(completion_file),
        "created_at": created_at,
        "monitor": monitor,
        "stage_plan_file": str(stage_plan_file) if stage_plan_file else None,
    })
    write_run_runtime(runtime_file, run_id=run_id, state="launching")
    atomic_write_json(index_root / f"{run_id}.json", {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "name": name,
        "owner_thread_id": thread_id,
        "spec_file": str(spec_file),
        "created_at": created_at,
    })
    return spec_file, runtime_file


def write_run_runtime(
    path: Path,
    *,
    run_id: str,
    state: str,
    worker_pid: int | None = None,
    target_pid: int | None = None,
    exit_code: int | None = None,
    error: str | None = None,
    completion_file: Path | None = None,
    goal_guard: dict[str, object] | None = None,
    target_identity: dict[str, object] | None = None,
    observer_mode: str | None = None,
    log_offset: int | None = None,
    completed_stages: list[str] | None = None,
    last_event: dict[str, object] | None = None,
) -> None:
    previous = read_json(path) if path.exists() else {}
    started_at = previous.get("started_at")
    if state == "running" and started_at is None:
        started_at = utc_now()
    terminal = state in {
        "completed", "cancelled", "detached", "observed_exit", "startup_failed", "state_failed"
    }
    atomic_write_json(path, {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "state": state,
        "worker_pid": worker_pid if worker_pid is not None else previous.get("worker_pid"),
        "target_pid": target_pid if target_pid is not None else previous.get("target_pid"),
        "started_at": started_at,
        "updated_at": utc_now(),
        "completed_at": utc_now() if terminal else previous.get("completed_at"),
        "exit_code": exit_code,
        "error": error,
        "completion_file": str(completion_file) if completion_file else previous.get("completion_file"),
        "goal_guard": goal_guard if goal_guard is not None else previous.get("goal_guard"),
        "target_identity": (
            target_identity if target_identity is not None else previous.get("target_identity")
        ),
        "observer_mode": observer_mode or previous.get("observer_mode", "owned"),
        "log_offset": log_offset if log_offset is not None else previous.get("log_offset", 0),
        "completed_stages": (
            completed_stages if completed_stages is not None else previous.get("completed_stages", [])
        ),
        "last_event": last_event if last_event is not None else previous.get("last_event"),
    })


def load_run_record(run_id: str) -> tuple[Path, dict[str, object], Path, dict[str, object]]:
    index_file = run_index_root() / f"{run_id}.json"
    index = read_json(index_file)
    if index.get("run_id") != run_id:
        raise RuntimeError(f"Run index has the wrong run_id: {index_file}")
    spec_file = Path(_required_text(index.get("spec_file"), "spec_file"))
    spec = read_json(spec_file)
    if spec.get("run_id") != run_id:
        raise RuntimeError(f"Run spec has the wrong run_id: {spec_file}")
    runtime_file = Path(_required_text(spec.get("runtime_file"), "runtime_file"))
    runtime = read_json(runtime_file)
    if runtime.get("run_id") != run_id:
        raise RuntimeError(f"Run runtime has the wrong run_id: {runtime_file}")
    return spec_file, spec, runtime_file, runtime


def show_run(run_id: str) -> dict[str, object]:
    _spec_file, spec, _runtime_file, runtime = load_run_record(run_id)
    return _run_view(spec, runtime)


def list_runs(*, thread_id: str, active_only: bool) -> dict[str, object]:
    runs: list[dict[str, object]] = []
    failures: dict[str, str] = {}
    root = run_index_root()
    for index_file in sorted(root.glob("*.json")) if root.exists() else []:
        try:
            index = read_json(index_file)
            if index.get("owner_thread_id") != thread_id:
                continue
            run = show_run(_required_text(index.get("run_id"), "run_id"))
            if active_only and run["state"] not in ACTIVE_STATES:
                continue
            runs.append(run)
        except Exception as error:
            failures[index_file.name] = f"{type(error).__name__}: {error}"
    runs.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return {"runs": runs, "failures": failures}


def _run_view(spec: dict[str, object], runtime: dict[str, object]) -> dict[str, object]:
    worker_pid = _optional_pid(runtime.get("worker_pid"))
    target_pid = _optional_pid(runtime.get("target_pid"))
    worker_alive = process_is_alive(worker_pid) if worker_pid else False
    target_alive = process_is_alive(target_pid) if target_pid else False
    state, health = _observed_state(str(runtime.get("state")), worker_alive, target_alive)
    completion_file = Path(_required_text(spec.get("completion_file"), "completion_file"))
    completion = read_json(completion_file) if completion_file.exists() else None
    if completion is not None:
        state = str(completion.get("terminal_state", "completed"))
        health = "terminal"
    log_file = Path(_required_text(spec.get("log_file"), "log_file"))
    log_stat = log_file.stat() if log_file.exists() else None
    return {
        "run_id": spec["run_id"],
        "name": spec.get("name"),
        "state": state,
        "health": health,
        "created_at": spec.get("created_at"),
        "started_at": runtime.get("started_at"),
        "completed_at": completion.get("completed_at") if completion else runtime.get("completed_at"),
        "elapsed_seconds": _elapsed_seconds(spec, runtime, completion),
        "worker_pid": worker_pid,
        "worker_alive": worker_alive,
        "target_pid": target_pid,
        "target_alive": target_alive,
        "exit_code": completion.get("exit_code") if completion else runtime.get("exit_code"),
        "error": completion.get("launch_error") if completion else runtime.get("error"),
        "cwd": spec.get("cwd"),
        "command": spec.get("command"),
        "log_file": str(log_file),
        "log_size_bytes": log_stat.st_size if log_stat else 0,
        "log_updated_at": (
            datetime.datetime.fromtimestamp(log_stat.st_mtime, datetime.timezone.utc).isoformat()
            if log_stat else None
        ),
        "monitor_policy": spec.get("monitor"),
        "monitor": completion.get("monitor") if completion else _monitor_runtime(spec),
        "delivery": completion.get("delivery") if completion else None,
        "goal_guard": completion.get("goal_guard") if completion else runtime.get("goal_guard"),
        "observer_mode": completion.get("observer_mode") if completion else runtime.get("observer_mode"),
        "exact_exit_code_available": (
            completion.get("exact_exit_code_available") if completion else runtime.get("observer_mode") != "adopted"
        ),
        "completed_stages": runtime.get("completed_stages", []),
        "next_stage": _next_stage(spec, runtime),
        "last_event": runtime.get("last_event"),
    }


def _observed_state(state: str, worker_alive: bool, target_alive: bool) -> tuple[str, str]:
    if state != "running":
        terminal = {"completed", "cancelled", "detached", "observed_exit", "startup_failed", "state_failed"}
        return state, "terminal" if state in terminal else "pending"
    if worker_alive:
        return state, "healthy"
    if target_alive:
        return "orphaned", "worker_missing"
    return "lost", "worker_and_target_missing"


def _elapsed_seconds(
    spec: dict[str, object],
    runtime: dict[str, object],
    completion: dict[str, object] | None,
) -> float | None:
    start_text = runtime.get("started_at") or spec.get("created_at")
    end_text = completion.get("completed_at") if completion else runtime.get("completed_at")
    if not isinstance(start_text, str):
        return None
    start = datetime.datetime.fromisoformat(start_text)
    end = datetime.datetime.fromisoformat(end_text) if isinstance(end_text, str) else datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (end - start).total_seconds())


def _monitor_runtime(spec: dict[str, object]) -> dict[str, object] | None:
    monitor = spec.get("monitor")
    if not isinstance(monitor, dict) or monitor.get("enabled") is not True:
        return monitor if isinstance(monitor, dict) else None
    runtime_file = monitor.get("runtime_file")
    if not isinstance(runtime_file, str):
        return monitor
    return {**monitor, **read_json(Path(runtime_file))}


def _next_stage(spec: dict[str, object], runtime: dict[str, object]) -> str | None:
    plan_file = spec.get("stage_plan_file")
    if not isinstance(plan_file, str):
        return None
    payload = read_json(Path(plan_file))
    stages = payload.get("stages")
    completed = runtime.get("completed_stages", [])
    if not isinstance(stages, list) or not isinstance(completed, list):
        raise RuntimeError("Run stage state is invalid")
    if len(completed) >= len(stages):
        return None
    stage = stages[len(completed)]
    if not isinstance(stage, dict) or not isinstance(stage.get("id"), str):
        raise RuntimeError("Run stage plan is invalid")
    return str(stage["id"])


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Run record has no valid {field}")
    return value


def _optional_pid(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
