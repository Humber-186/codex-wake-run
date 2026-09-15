"""Minimal synchronous client for the Codex App Server JSONL protocol."""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TextIO

from wake_run_platform import build_codex_invocation

APP_SERVER_CALL_TIMEOUT = 10.0
CLIENT_INFO = {
    "name": "wake_run",
    "title": "Wake Run",
    "version": "1.0.0",
}


@dataclass(frozen=True)
class AppServerConfig:
    codex_bin: str
    call_timeout: float

    @classmethod
    def from_environment(cls, codex_bin: str) -> "AppServerConfig":
        timeout = float(os.environ.get("WAKE_RUN_GOAL_TIMEOUT", APP_SERVER_CALL_TIMEOUT))
        if not math.isfinite(timeout) or timeout <= 0:
            raise RuntimeError("WAKE_RUN_GOAL_TIMEOUT must be a finite number greater than zero")
        return cls(codex_bin=codex_bin, call_timeout=timeout)


class AppServerClient:
    """Own one stdio App Server process and correlate JSON-RPC responses by id."""

    def __init__(self, config: AppServerConfig) -> None:
        self._config = config
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, object] | Exception] = queue.Queue()
        self._stderr: list[str] = []
        self._next_id = 1

    def __enter__(self) -> "AppServerClient":
        self._process = subprocess.Popen(
            build_codex_invocation(self._config.codex_bin, ["app-server", "--stdio"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        threading.Thread(target=self._read_messages, args=(self._process.stdout,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self._process.stderr,), daemon=True).start()
        try:
            self.request("initialize", {"clientInfo": CLIENT_INFO})
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._process is None:
            return
        process = self._require_process()
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=self._config.call_timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=self._config.call_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        self._process = None

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self._config.call_timeout
        while True:
            message = self._next_message(method, deadline)
            if message.get("id") != request_id:
                continue
            error = message.get("error")
            if error is not None:
                raise RuntimeError(f"Codex App Server {method} failed: {error}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"Codex App Server {method} returned an invalid result")
            return result

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._send({"method": method, "params": params})

    def _send(self, message: dict[str, object]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise RuntimeError("Codex App Server stdin is unavailable")
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def _next_message(self, method: str, deadline: float) -> dict[str, object]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"Codex App Server {method} timed out")
        try:
            item = self._messages.get(timeout=remaining)
        except queue.Empty as exc:
            detail = "".join(self._stderr).strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Codex App Server {method} timed out{suffix}") from exc
        if isinstance(item, Exception):
            raise RuntimeError(f"Codex App Server protocol failed: {item}") from item
        return item

    def _read_messages(self, stream: TextIO) -> None:
        try:
            for line in stream:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise RuntimeError("App Server emitted a non-object JSON message")
                self._messages.put(message)
            self._messages.put(RuntimeError("App Server stdout closed"))
        except Exception as error:
            self._messages.put(error)

    def _read_stderr(self, stream: TextIO) -> None:
        for line in stream:
            self._stderr.append(line)

    def _require_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            raise RuntimeError("Codex App Server client is not started")
        return self._process


class CodexGoalRpc:
    """Goal RPC adapter backed by one detached stdio App Server."""

    def __init__(self, codex_bin: str) -> None:
        self._client = AppServerClient(AppServerConfig.from_environment(codex_bin))

    def __enter__(self) -> "CodexGoalRpc":
        self._client.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.__exit__(*exc)

    def get_goal(self, thread_id: str) -> dict[str, object] | None:
        result = self._client.request("thread/goal/get", {"threadId": thread_id})
        goal = result.get("goal")
        if goal is not None and not isinstance(goal, dict):
            raise RuntimeError("thread/goal/get returned an invalid goal")
        return goal

    def set_status(self, thread_id: str, status: str) -> dict[str, object]:
        result = self._client.request(
            "thread/goal/set", {"threadId": thread_id, "status": status}
        )
        goal = result.get("goal")
        if not isinstance(goal, dict):
            raise RuntimeError("thread/goal/set returned an invalid goal")
        return goal
