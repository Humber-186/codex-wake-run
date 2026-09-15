"""Cross-platform target process-tree lifecycle helpers."""

from __future__ import annotations

import os
import signal
import subprocess
from types import FrameType

PROCESS_TREE_STOP_TIMEOUT = 5
WINDOWS_FORCE_KILL_FLAGS = ("/T", "/F")


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
