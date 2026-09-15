"""Durable stop and detach commands for active workers."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from wake_run_registry import load_run_record
from wake_run_state import atomic_write_json, process_is_alive, read_json, utc_now

CONTROL_SCHEMA_VERSION = 1
CONTROL_WAIT_SECONDS = 15
CONTROL_POLL_SECONDS = 0.05
CONTROL_ACTIONS = frozenset({"stop", "detach"})


def control_paths(log_file: Path) -> tuple[Path, Path]:
    return (
        log_file.with_name(f"{log_file.stem}.commands"),
        log_file.with_name(f"{log_file.stem}.acknowledgements"),
    )


def issue_control(run_id: str, action: str, *, thread_id: str) -> dict[str, object]:
    if action not in CONTROL_ACTIONS:
        raise RuntimeError(f"Unsupported control action: {action}")
    _spec_file, spec, _runtime_file, runtime = load_run_record(run_id)
    if spec.get("owner_thread_id") != thread_id:
        raise RuntimeError(f"Run {run_id} is owned by another Codex thread")
    if runtime.get("state") != "running":
        raise RuntimeError(f"Run {run_id} is not controllable in state {runtime.get('state')}")
    worker_pid = runtime.get("worker_pid")
    if not isinstance(worker_pid, int) or not process_is_alive(worker_pid):
        raise RuntimeError(f"Run {run_id} worker is not alive; use adopt before {action}")
    log_file = Path(_required_text(spec.get("log_file"), "log_file"))
    command_dir, ack_dir = control_paths(log_file)
    command_id = uuid.uuid4().hex
    command_file = command_dir / f"{command_id}.json"
    ack_file = ack_dir / f"{command_id}.json"
    atomic_write_json(command_file, {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "command_id": command_id,
        "run_id": run_id,
        "action": action,
        "created_at": utc_now(),
    })
    return _wait_for_ack(ack_file, run_id=run_id, action=action)


def pending_controls(log_file: Path) -> tuple[tuple[Path, dict[str, object]], ...]:
    command_dir, ack_dir = control_paths(log_file)
    pending: list[tuple[Path, dict[str, object]]] = []
    for command_file in sorted(command_dir.glob("*.json")) if command_dir.exists() else []:
        if (ack_dir / command_file.name).exists():
            continue
        payload = read_json(command_file)
        _validate_command(payload, command_file)
        pending.append((command_file, payload))
    return tuple(pending)


def acknowledge_control(
    log_file: Path,
    command_file: Path,
    command: dict[str, object],
    *,
    status: str,
    error: str | None = None,
) -> None:
    _command_dir, ack_dir = control_paths(log_file)
    atomic_write_json(ack_dir / command_file.name, {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "command_id": command["command_id"],
        "run_id": command["run_id"],
        "action": command["action"],
        "status": status,
        "error": error,
        "acknowledged_at": utc_now(),
    })


def _wait_for_ack(path: Path, *, run_id: str, action: str) -> dict[str, object]:
    deadline = time.monotonic() + CONTROL_WAIT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            ack = read_json(path)
            if ack.get("run_id") != run_id or ack.get("action") != action:
                raise RuntimeError(f"Control acknowledgement identity mismatch: {path}")
            if ack.get("status") == "error":
                raise RuntimeError(f"Run {run_id} {action} failed: {ack.get('error')}")
            return ack
        time.sleep(CONTROL_POLL_SECONDS)
    raise RuntimeError(f"Run {run_id} worker did not acknowledge {action} within {CONTROL_WAIT_SECONDS}s")


def _validate_command(payload: dict[str, object], path: Path) -> None:
    required = {"schema_version", "command_id", "run_id", "action", "created_at"}
    if set(payload) != required or payload.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise RuntimeError(f"Invalid control command: {path}")
    if payload.get("action") not in CONTROL_ACTIONS:
        raise RuntimeError(f"Invalid control action in {path}")
    for field in ("command_id", "run_id", "created_at"):
        _required_text(payload.get(field), field)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Control state has no valid {field}")
    return value
