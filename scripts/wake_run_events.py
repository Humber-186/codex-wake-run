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
    return persisted_stage_state(log_file)[0]


def persisted_stage_state(log_file: Path) -> tuple[tuple[str, ...], int]:
    values: list[str] = []
    offset = 0
    pattern = f"{log_file.stem}.stage-*.event.json"
    for path in sorted(log_file.parent.glob(pattern)):
        payload = read_json(path)
        stage_id = payload.get("stage_id")
        if not isinstance(stage_id, str) or not stage_id:
            raise RuntimeError(f"Stage event has no valid stage_id: {path}")
        event_offset = payload.get("log_offset")
        if not isinstance(event_offset, int) or event_offset < offset:
            raise RuntimeError(f"Stage event has an invalid log_offset: {path}")
        values.append(stage_id)
        offset = event_offset
    return tuple(values), offset


def stage_delivery_summary(
    log_file: Path,
    *,
    delivery_errors: list[str] | None = None,
) -> dict[str, object]:
    total = delivered = failed = 0
    last_error: str | None = None
    pattern = f"{log_file.stem}.stage-*.event.json"
    for path in sorted(log_file.parent.glob(pattern)):
        delivery = read_json(path).get("delivery")
        if not isinstance(delivery, dict):
            raise RuntimeError(f"Missing delivery state in {path}")
        total += 1
        if delivery.get("state") == DELIVERY_DELIVERED:
            delivered += 1
            continue
        error = delivery.get("last_error")
        if isinstance(error, str) and error:
            failed += 1
            last_error = error
    if delivery_errors:
        failed = max(failed, len(delivery_errors))
        last_error = delivery_errors[-1]
    return {
        "total": total,
        "delivered": delivered,
        "pending": total - delivered,
        "failed": failed,
        "last_error": last_error,
    }


def event_is_delivered(path: Path) -> bool:
    delivery = read_json(path).get("delivery")
    if not isinstance(delivery, dict):
        raise RuntimeError(f"Missing delivery state in {path}")
    return delivery.get("state") == DELIVERY_DELIVERED
