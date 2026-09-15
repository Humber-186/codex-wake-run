"""Goal lease schema and holder-state helpers."""

from __future__ import annotations

from pathlib import Path

from wake_run_state import atomic_write_json, process_is_alive, read_json, utc_now

LEASE_SCHEMA_VERSION = 2
LEGACY_LEASE_SCHEMA_VERSION = 1
LEASE_PHASE_PAUSED = "paused"
LEASE_PHASE_ORPHANED = "orphaned"
LEASE_PHASE_RESTORED = "restored"
LEASE_PHASE_SKIPPED = "skipped_conflict"
TERMINAL_PHASES = frozenset({LEASE_PHASE_RESTORED, LEASE_PHASE_SKIPPED})
HOLDER_PHASE_PREPARED = "prepared"
HOLDER_PHASE_SPAWNING = "spawning"
HOLDER_PHASE_RUNNING = "running"
HOLDER_PHASES = frozenset({
    HOLDER_PHASE_PREPARED,
    HOLDER_PHASE_SPAWNING,
    HOLDER_PHASE_RUNNING,
})
ORPHANABLE_HOLDER_PHASES = frozenset({HOLDER_PHASE_SPAWNING, HOLDER_PHASE_RUNNING})


def holder(run_id: str, worker_pid: int, completion_file: Path | None) -> dict[str, object]:
    if not run_id:
        raise RuntimeError("Goal lease holder run_id must not be empty")
    if worker_pid < 0:
        raise RuntimeError("Goal lease holder worker_pid must not be negative")
    return {
        "run_id": run_id,
        "worker_pid": worker_pid,
        "completion_file": str(completion_file.resolve()) if completion_file else None,
        "phase": HOLDER_PHASE_PREPARED,
        "target_pid": None,
    }


def read_lease(path: Path, thread_id: str) -> dict[str, object] | None:
    if not path.exists():
        return None
    lease = read_json(path)
    version = lease.get("schema_version")
    if version == LEGACY_LEASE_SCHEMA_VERSION:
        lease = _upgrade_legacy_lease(lease)
    elif version != LEASE_SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported Goal lease schema in {path}")
    if lease.get("thread_id") != thread_id:
        raise RuntimeError(f"Goal lease thread mismatch in {path}")
    lease_holders(lease)
    return lease


def lease_holders(lease: dict[str, object]) -> list[dict[str, object]]:
    holders = lease.get("holders")
    if not isinstance(holders, list) or not all(_valid_holder(item) for item in holders):
        raise RuntimeError("Goal lease holders are invalid")
    return [dict(item) for item in holders if isinstance(item, dict)]


def holder_is_abandoned(holder: dict[str, object], log_dir: Path | None = None) -> bool:
    completion = holder.get("completion_file")
    if not isinstance(completion, str):
        return False
    completion_path = Path(completion)
    if log_dir is not None and completion_path.parent != log_dir:
        return False
    if completion_path.exists():
        return False
    worker_pid = holder.get("worker_pid")
    return isinstance(worker_pid, int) and not process_is_alive(worker_pid)


def holder_is_orphaned(holder: dict[str, object], log_dir: Path | None = None) -> bool:
    return holder.get("phase") in ORPHANABLE_HOLDER_PHASES and holder_is_abandoned(
        holder, log_dir
    )


def persist_orphaned(path: Path, lease: dict[str, object]) -> None:
    atomic_write_json(path, {
        **lease,
        "phase": LEASE_PHASE_ORPHANED,
        "updated_at": utc_now(),
    })


def raise_if_orphaned(path: Path, lease: dict[str, object]) -> None:
    if lease.get("phase") == LEASE_PHASE_ORPHANED:
        raise RuntimeError("Goal lease is orphaned and must remain paused")
    if not any(holder_is_orphaned(item) for item in lease_holders(lease)):
        return
    persist_orphaned(path, lease)
    raise RuntimeError("Goal lease has an orphaned holder and must remain paused")


def _valid_holder(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    completion_file = value.get("completion_file")
    target_pid = value.get("target_pid")
    return (
        isinstance(value.get("run_id"), str)
        and isinstance(value.get("worker_pid"), int)
        and value.get("phase") in HOLDER_PHASES
        and (target_pid is None or isinstance(target_pid, int))
        and (completion_file is None or isinstance(completion_file, str))
    )


def _upgrade_legacy_lease(lease: dict[str, object]) -> dict[str, object]:
    holders = lease.get("holders")
    if not isinstance(holders, list):
        raise RuntimeError("Legacy Goal lease holders are invalid")
    upgraded = []
    for item in holders:
        if not isinstance(item, dict) or not isinstance(item.get("target_started"), bool):
            raise RuntimeError("Legacy Goal lease holder is invalid")
        phase = HOLDER_PHASE_RUNNING if item["target_started"] else HOLDER_PHASE_PREPARED
        converted = {**item, "phase": phase, "target_pid": None}
        converted.pop("target_started", None)
        upgraded.append(converted)
    return {**lease, "schema_version": LEASE_SCHEMA_VERSION, "holders": upgraded}
