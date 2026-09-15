"""Persistent non-terminal wake events."""

from __future__ import annotations

from pathlib import Path

from wake_run_state import (
    DELIVERY_DELIVERED,
    DELIVERY_PENDING,
    atomic_write_json,
    read_json,
    utc_now,
)

EVENT_SCHEMA_VERSION = 1


def stage_event_path(log_file: Path, sequence: int) -> Path:
    return log_file.with_name(f"{log_file.stem}.stage-{sequence:06d}.event.json")


def create_stage_event(
    log_file: Path,
    *,
    sequence: int,
    run_id: str,
    thread_id: str,
    command: str,
    stage_id: str,
    matched_line: str,
    log_offset: int,
) -> Path:
    destination = stage_event_path(log_file, sequence)
    if destination.exists():
        existing = read_json(destination)
        if existing.get("run_id") != run_id or existing.get("stage_id") != stage_id:
            raise RuntimeError(f"Stage event identity conflict: {destination}")
        return destination
    atomic_write_json(destination, {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_type": "stage",
        "run_id": run_id,
        "thread_id": thread_id,
        "command": command,
        "stage_id": stage_id,
        "matched_line": matched_line,
        "log_offset": log_offset,
        "occurred_at": utc_now(),
        "wake_id": f"{run_id}:stage:{stage_id}",
        "log_file": str(log_file),
        "delivery": {
            "state": DELIVERY_PENDING,
            "attempts": 0,
            "last_attempt_at": None,
            "last_error": None,
            "delivered_at": None,
            "delivery_pid": None,
        },
    })
    return destination


def stage_event_files(log_dir: Path) -> list[Path]:
    return sorted(log_dir.glob("*.stage-*.event.json"))


def delivered_stage_ids(log_file: Path) -> tuple[str, ...]:
    values: list[str] = []
    pattern = f"{log_file.stem}.stage-*.event.json"
    for path in sorted(log_file.parent.glob(pattern)):
        payload = read_json(path)
        stage_id = payload.get("stage_id")
        if not isinstance(stage_id, str) or not stage_id:
            raise RuntimeError(f"Stage event has no valid stage_id: {path}")
        values.append(stage_id)
    return tuple(values)


def event_is_delivered(path: Path) -> bool:
    delivery = read_json(path).get("delivery")
    if not isinstance(delivery, dict):
        raise RuntimeError(f"Missing delivery state in {path}")
    return delivery.get("state") == DELIVERY_DELIVERED
