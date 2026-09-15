"""Cross-platform target process-tree lifecycle helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from types import FrameType

PROCESS_TREE_STOP_TIMEOUT = 5
WINDOWS_FORCE_KILL_FLAGS = ("/T", "/F")
LINUX_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def process_identity(pid: int, *, platform: str | None = None) -> dict[str, object]:
    active_platform = platform or os.name
    if active_platform != "posix" or not Path("/proc").is_dir():
        raise RuntimeError("Process identity capture for adopt is supported only on Linux")
    _state, start_time = _linux_process_state_and_start(pid)
    return {
        "platform": "linux",
        "pid": pid,
        "boot_id": LINUX_BOOT_ID.read_text(encoding="utf-8").strip(),
        "start_time_ticks": start_time,
    }


def optional_process_identity(pid: int) -> dict[str, object] | None:
    if os.name != "posix" or not Path("/proc").is_dir():
        return None
    try:
        return process_identity(pid)
    except FileNotFoundError:
        return None


def process_identity_matches(identity: dict[str, object]) -> bool:
    pid = identity.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeError("Persisted target identity has no valid pid")
    if identity.get("platform") != "linux":
        raise RuntimeError("Persisted target identity is not a Linux identity")
    try:
        state, start_time = _linux_process_state_and_start(pid)
        boot_id = LINUX_BOOT_ID.read_text(encoding="utf-8").strip()
        return (
            state != "Z"
            and identity.get("boot_id") == boot_id
            and identity.get("start_time_ticks") == start_time
        )
    except FileNotFoundError:
        return False


def _linux_process_state_and_start(pid: int) -> tuple[str, int]:
    stat_path = Path("/proc") / str(pid) / "stat"
    stat_text = stat_path.read_text(encoding="utf-8")
    _prefix, separator, suffix = stat_text.rpartition(") ")
    fields = suffix.split()
    if not separator or len(fields) <= 19:
        raise RuntimeError(f"Cannot parse Linux process identity from {stat_path}")
    return fields[0], int(fields[19])


def terminate_identified_process_tree(
    identity: dict[str, object],
    *,
    timeout: float = PROCESS_TREE_STOP_TIMEOUT,
) -> None:
    if not process_identity_matches(identity):
        return
    pid = int(identity["pid"])
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while process_identity_matches(identity):
        if time.monotonic() >= deadline:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return
        time.sleep(0.05)


def target_popen_kwargs(*, platform: str | None = None) -> dict[str, object]:
    active_platform = platform or os.name
    if active_platform == "nt":
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", None)
        if creation_flag is None:
            raise RuntimeError("Python does not expose CREATE_NEW_PROCESS_GROUP on Windows")
        return {"creationflags": creation_flag}
    return {"start_new_session": True}


def _terminate_posix_tree(process: subprocess.Popen[bytes], timeout: float) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _terminate_windows_tree(process: subprocess.Popen[bytes], timeout: float) -> None:
    result = subprocess.run(
        ["taskkill", "/PID", str(process.pid), *WINDOWS_FORCE_KILL_FLAGS],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0 and process.poll() is None:
        raise RuntimeError(f"taskkill failed for process tree {process.pid} (exit {result.returncode})")
    process.wait(timeout=timeout)


def terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    timeout: float = PROCESS_TREE_STOP_TIMEOUT,
    platform: str | None = None,
) -> None:
    if process.poll() is not None:
        return
    active_platform = platform or os.name
    if active_platform == "nt":
        _terminate_windows_tree(process, timeout)
        return
    _terminate_posix_tree(process, timeout)


class ProcessTreeSignalGuard:
    """Forward worker termination signals to its active target process tree."""

    def __init__(self, *, platform: str | None = None) -> None:
        self._platform = platform or os.name
        self._process: subprocess.Popen[bytes] | None = None
        self._previous: dict[int, object] = {}

    def __enter__(self) -> "ProcessTreeSignalGuard":
        if self._platform == "nt":
            return self
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            self._previous[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, self._handle_signal)
        return self

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    def _handle_signal(self, signal_number: int, _frame: FrameType | None) -> None:
        cleanup_error: Exception | None = None
        if self._process is not None:
            try:
                terminate_process_tree(self._process)
            except Exception as error:
                cleanup_error = error
        if cleanup_error is not None:
            raise SystemExit(
                f"wake-run received signal {signal_number}; target cleanup failed: {cleanup_error}"
            )
        raise SystemExit(128 + signal_number)

    def __exit__(self, *_args: object) -> None:
        if self._platform == "nt":
            return
        for signal_number, previous in self._previous.items():
            signal.signal(signal_number, previous)
