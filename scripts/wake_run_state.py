"""Durable state and private-file helpers for wake-run."""

from __future__ import annotations

import ctypes
import datetime
import errno
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DELIVERY_PENDING = "pending"
DELIVERY_IN_PROGRESS = "delivering"
DELIVERY_DELIVERED = "delivered"
DELIVERY_STATES = frozenset({DELIVERY_PENDING, DELIVERY_IN_PROGRESS, DELIVERY_DELIVERED})
GOAL_RELEASE_NOT_NEEDED = "not_needed"
GOAL_RELEASE_PENDING = "pending"
GOAL_RELEASE_RETRYING = "retrying"
GOAL_RELEASE_RESTORED = "restored"
GOAL_RELEASE_SKIPPED = "skipped_conflict"
GOAL_RELEASE_STATES = frozenset({
    GOAL_RELEASE_NOT_NEEDED,
    GOAL_RELEASE_PENDING,
    GOAL_RELEASE_RETRYING,
    GOAL_RELEASE_RESTORED,
    GOAL_RELEASE_SKIPPED,
})
SCHEMA_VERSION = 2
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87


class DeliveryInProgressError(RuntimeError):
    """Raised when another live process owns an event's delivery lock."""


class ProcessLockError(RuntimeError):
    """Raised when another live process owns a state lock."""


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(PRIVATE_DIR_MODE)


def open_private_log(path: Path):
    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, PRIVATE_FILE_MODE)
    if os.name != "nt":
        os.chmod(path, PRIVATE_FILE_MODE)
    return os.fdopen(descriptor, "ab", buffering=0)


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_EXCL | os.O_CREAT | os.O_WRONLY, PRIVATE_FILE_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return payload


def completion_path(log_file: Path) -> Path:
    return log_file.with_suffix(".completion.json")


def write_startup_status(
    path: Path,
    *,
    state: str,
    run_id: str,
    worker_pid: int,
    process_pid: int | None = None,
    error: str | None = None,
    goal_guard: dict[str, object] | None = None,
) -> None:
    atomic_write_json(path, {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "run_id": run_id,
        "worker_pid": worker_pid,
        "process_pid": process_pid,
        "updated_at": utc_now(),
        "error": error,
        "goal_guard": goal_guard,
    })


def create_completion_event(
    log_file: Path,
    *,
    run_id: str,
    thread_id: str,
    command: str,
    exit_code: int | None,
    launch_error: str | None,
    wake_id: str,
    duration_seconds: float | None = None,
    user_seconds: float | None = None,
    system_seconds: float | None = None,
    execution_attempts: list[dict[str, object]] | None = None,
    monitor_plan_file: str | None = None,
    monitor_triage: list[dict[str, object]] | None = None,
    monitor_status: str | None = None,
    monitor_error: str | None = None,
    goal_guard: dict[str, object] | None = None,
    terminal_state: str = "completed",
    observer_mode: str = "owned",
    stage_delivery: dict[str, object] | None = None,
) -> Path:
    guard = goal_guard or {"mode": "not_needed", "verified": True, "lease_id": None}
    release_state = (
        GOAL_RELEASE_PENDING if guard.get("lease_id") else GOAL_RELEASE_NOT_NEEDED
    )
    destination = completion_path(log_file)
    atomic_write_json(destination, {
        "schema_version": SCHEMA_VERSION,
        "event_type": "process_exit",
        "run_id": run_id,
        "thread_id": thread_id,
        "command": command,
        "exit_code": exit_code,
        "launch_error": launch_error,
        "duration_seconds": duration_seconds,
        "user_seconds": user_seconds,
        "system_seconds": system_seconds,
        "completed_at": utc_now(),
        "terminal_state": terminal_state,
        "observer_mode": observer_mode,
        "exact_exit_code_available": observer_mode != "adopted" and exit_code is not None,
        "stage_delivery": stage_delivery or {
            "total": 0,
            "delivered": 0,
            "pending": 0,
            "failed": 0,
            "last_error": None,
        },
        "wake_id": wake_id,
        "log_file": str(log_file),
        "execution_attempts": execution_attempts or [],
        "monitor": {
            "enabled": monitor_plan_file is not None,
            "status": monitor_status or ("pending" if monitor_plan_file else "disabled"),
            "plan_file": monitor_plan_file,
            "triage": monitor_triage or [],
            "error": monitor_error,
        },
        "goal_guard": guard,
        "goal_release": {
            "state": release_state,
            "attempts": 0,
            "last_attempt_at": None,
            "last_error": None,
            "released_at": None,
        },
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


def update_delivery(
    path: Path,
    *,
    state: str,
    attempts: int,
    last_error: str | None,
) -> None:
    if state not in DELIVERY_STATES:
        raise RuntimeError(f"Invalid delivery state: {state}")
    if attempts < 0:
        raise RuntimeError(f"Delivery attempts must not be negative: {attempts}")
    payload = read_json(path)
    prior = payload.get("delivery")
    if not isinstance(prior, dict):
        raise RuntimeError(f"Missing delivery state in {path}")
    delivered_at = utc_now() if state == DELIVERY_DELIVERED else prior.get("delivered_at")
    delivery = {
        **prior,
        "state": state,
        "attempts": attempts,
        "last_attempt_at": utc_now(),
        "last_error": last_error,
        "delivered_at": delivered_at,
        "delivery_pid": os.getpid() if state == DELIVERY_IN_PROGRESS else None,
    }
    atomic_write_json(path, {**payload, "delivery": delivery})


def update_stage_delivery(path: Path, stage_delivery: dict[str, object]) -> None:
    payload = read_json(path)
    if payload.get("event_type") != "process_exit":
        raise RuntimeError(f"Not a completion event: {path}")
    atomic_write_json(path, {**payload, "stage_delivery": stage_delivery})


def update_goal_release(
    path: Path,
    *,
    state: str,
    attempts: int,
    last_error: str | None,
) -> None:
    if state not in GOAL_RELEASE_STATES:
        raise RuntimeError(f"Invalid Goal release state: {state}")
    if attempts < 0:
        raise RuntimeError(f"Goal release attempts must not be negative: {attempts}")
    payload = read_json(path)
    prior = payload.get("goal_release")
    if not isinstance(prior, dict):
        raise RuntimeError(f"Missing Goal release state in {path}")
    released_at = utc_now() if state in {GOAL_RELEASE_RESTORED, GOAL_RELEASE_SKIPPED} else None
    goal_release = {
        **prior,
        "state": state,
        "attempts": attempts,
        "last_attempt_at": utc_now(),
        "last_error": last_error,
        "released_at": released_at or prior.get("released_at"),
    }
    atomic_write_json(path, {**payload, "goal_release": goal_release})


def completion_files(log_dir: Path) -> list[Path]:
    return sorted(log_dir.glob("*.completion.json"))


def completion_is_undelivered(path: Path) -> bool:
    payload = read_json(path)
    delivery = payload.get("delivery")
    if not isinstance(delivery, dict):
        raise RuntimeError(f"Missing delivery state in {path}")
    state = delivery.get("state")
    if state not in DELIVERY_STATES:
        raise RuntimeError(f"Invalid delivery state in {path}: {state}")
    goal_release = completion_goal_release(payload, path)
    release_state = goal_release.get("state")
    if release_state not in GOAL_RELEASE_STATES:
        raise RuntimeError(f"Invalid Goal release state in {path}: {release_state}")
    release_pending = release_state in {GOAL_RELEASE_PENDING, GOAL_RELEASE_RETRYING}
    return state != DELIVERY_DELIVERED or release_pending


def completion_goal_release(
    payload: dict[str, object],
    path: Path,
) -> dict[str, object]:
    goal_release = payload.get("goal_release")
    if isinstance(goal_release, dict):
        return goal_release
    if payload.get("schema_version") == 1:
        return {
            "state": GOAL_RELEASE_NOT_NEEDED,
            "attempts": 0,
            "last_attempt_at": None,
            "last_error": None,
            "released_at": None,
        }
    raise RuntimeError(f"Missing Goal release state in {path}")


def _windows_process_is_alive(pid: int) -> bool:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_INVALID_PARAMETER:
            return False
        if error == ERROR_ACCESS_DENIED:
            return True
        raise ctypes.WinError(error)
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    stat_path = Path("/proc") / str(pid) / "stat"
    if stat_path.exists():
        try:
            _prefix, separator, suffix = stat_path.read_text(encoding="utf-8").rpartition(") ")
            if separator and suffix.split()[0] == "Z":
                return False
        except FileNotFoundError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_is_alive(pid: int) -> bool:
    return _process_is_alive(pid)


@contextmanager
def process_lock(
    lock_file: Path,
    description: str,
    *,
    blocking: bool = False,
) -> Iterator[None]:
    ensure_private_directory(lock_file.parent)
    descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, PRIVATE_FILE_MODE)
    if os.name != "nt":
        os.chmod(lock_file, PRIVATE_FILE_MODE)
    acquired = False
    try:
        _acquire_os_lock(descriptor, description, lock_file, blocking=blocking)
        acquired = True
        payload = json.dumps({"pid": os.getpid(), "created_at": utc_now()}).encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        yield
    finally:
        if acquired:
            _release_os_lock(descriptor)
        os.close(descriptor)


def _acquire_os_lock(
    descriptor: int,
    description: str,
    path: Path,
    *,
    blocking: bool,
) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(descriptor, mode, 1)
            return
        import fcntl

        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(descriptor, flags)
    except (BlockingIOError, OSError) as error:
        if error.errno not in {errno.EACCES, errno.EAGAIN}:
            raise
        raise ProcessLockError(f"{description} is already locked: {path}") from error


def _release_os_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def delivery_lock(completion_file: Path) -> Iterator[None]:
    try:
        with process_lock(completion_file.with_suffix(".delivery.lock"), "Delivery"):
            yield
    except ProcessLockError as error:
        raise DeliveryInProgressError(str(error)) from error
