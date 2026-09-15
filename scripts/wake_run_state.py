"""Durable state and private-file helpers for wake-run."""

from __future__ import annotations

import ctypes
import datetime
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
SCHEMA_VERSION = 1
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87


class DeliveryInProgressError(RuntimeError):
    """Raised when another live process owns an event's delivery lock."""


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
) -> None:
    atomic_write_json(path, {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "run_id": run_id,
        "worker_pid": worker_pid,
        "process_pid": process_pid,
        "updated_at": utc_now(),
        "error": error,
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
    monitor_error: str | None = None,
) -> Path:
    destination = completion_path(log_file)
    atomic_write_json(destination, {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "thread_id": thread_id,
        "command": command,
        "exit_code": exit_code,
        "launch_error": launch_error,
        "duration_seconds": duration_seconds,
        "user_seconds": user_seconds,
        "system_seconds": system_seconds,
        "completed_at": utc_now(),
        "wake_id": wake_id,
        "log_file": str(log_file),
        "execution_attempts": execution_attempts or [],
        "monitor": {
            "enabled": monitor_plan_file is not None,
            "plan_file": monitor_plan_file,
            "triage": monitor_triage or [],
            "error": monitor_error,
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
    return state != DELIVERY_DELIVERED


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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def delivery_lock(completion_file: Path) -> Iterator[None]:
    lock_file = completion_file.with_suffix(".delivery.lock")
    while True:
        try:
            descriptor = os.open(lock_file, os.O_EXCL | os.O_CREAT | os.O_WRONLY, PRIVATE_FILE_MODE)
            break
        except FileExistsError:
            owner = read_json(lock_file)
            owner_pid = owner.get("pid")
            if isinstance(owner_pid, int) and _process_is_alive(owner_pid):
                raise DeliveryInProgressError(f"Delivery already owned by process {owner_pid}: {completion_file}")
            lock_file.unlink()

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "created_at": utc_now()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        lock_file.unlink(missing_ok=True)
