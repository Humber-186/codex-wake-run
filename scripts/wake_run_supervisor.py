"""Live process supervision, stage scanning, and control handling."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from wake_run_control import acknowledge_control, pending_controls
from wake_run_events import create_stage_event, delivered_stage_ids
from wake_run_goal import GoalGuardContext, cancel_goal_guard
from wake_run_models import WorkerRequest
from wake_run_process import terminate_process_tree
from wake_run_registry import write_run_runtime
from wake_run_stages import StageMatch, StageScanner, load_stage_plan

SUPERVISOR_INTERVAL_SECONDS = 0.2
StageDelivery = Callable[[Path, str], None]
WaitProcess = Callable[[subprocess.Popen[bytes]], int]


class StageDeliveryPump:
    def __init__(self, deliver: StageDelivery, codex_bin: str) -> None:
        self._deliver = deliver
        self._codex_bin = codex_bin
        self._queue: queue.Queue[Path | None] = queue.Queue()
        self.errors: list[str] = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, path: Path) -> None:
        self._queue.put(path)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join()

    def _run(self) -> None:
        while True:
            path = self._queue.get()
            try:
                if path is None:
                    return
                self._deliver(path, self._codex_bin)
            except Exception as error:
                assert path is not None
                self.errors.append(f"{path.name}: {type(error).__name__}: {error}")
            finally:
                self._queue.task_done()


def supervise_owned_process(
    request: WorkerRequest,
    process: subprocess.Popen[bytes],
    *,
    initial_log_offset: int,
    goal_guard: dict[str, object],
    deliver_stage: StageDelivery,
    wait_process: WaitProcess,
) -> tuple[int | None, str, str | None]:
    scanner = _scanner(request, initial_log_offset)
    pump = StageDeliveryPump(deliver_stage, request.codex_bin)
    wait_done, wait_result = _start_waiter(process, wait_process)
    while True:
        _scan_stages(request, scanner, pump, final=False)
        if wait_done.is_set():
            error = wait_result.get("error")
            if isinstance(error, Exception):
                raise error
            return_code = wait_result.get("exit_code")
            if not isinstance(return_code, int):
                raise RuntimeError("Process waiter returned no exit code")
            _scan_stages(request, scanner, pump, final=True)
            pump.close()
            delivery_error = "; ".join(pump.errors) if pump.errors else None
            return return_code, "completed", delivery_error
        action = _handle_controls(request, process, goal_guard)
        if action == "detached":
            return None, action, None
        if action == "cancelled":
            _scan_stages(request, scanner, pump, final=True)
            pump.close()
            error = "; ".join(pump.errors) if pump.errors else None
            return process.returncode, action, error
        time.sleep(SUPERVISOR_INTERVAL_SECONDS)


def _start_waiter(
    process: subprocess.Popen[bytes],
    wait_process: WaitProcess,
) -> tuple[threading.Event, dict[str, object]]:
    done = threading.Event()
    result: dict[str, object] = {}

    def wait() -> None:
        try:
            result["exit_code"] = wait_process(process)
        except Exception as error:
            result["error"] = error
        finally:
            done.set()

    threading.Thread(target=wait, daemon=True).start()
    return done, result


def _scanner(request: WorkerRequest, initial_offset: int) -> StageScanner | None:
    if request.stage_plan_file is None:
        return None
    rules = load_stage_plan(request.stage_plan_file)
    completed = delivered_stage_ids(request.log_file)
    offset = initial_offset
    if request.runtime_file is not None and request.runtime_file.exists():
        from wake_run_state import read_json

        runtime = read_json(request.runtime_file)
        value = runtime.get("log_offset")
        if isinstance(value, int) and value >= initial_offset:
            offset = value
    return StageScanner(rules, completed=completed, offset=offset)


def _scan_stages(
    request: WorkerRequest,
    scanner: StageScanner | None,
    pump: StageDeliveryPump,
    *,
    final: bool,
) -> None:
    if scanner is None:
        return
    prior_offset = scanner.offset
    prior_count = len(scanner.completed)
    for match in scanner.scan(request.log_file, final=final):
        event_file = _persist_match(request, scanner, match)
        pump.submit(event_file)
    if scanner.offset != prior_offset and len(scanner.completed) == prior_count:
        _record_stage_runtime(request, scanner, None)


def _persist_match(request: WorkerRequest, scanner: StageScanner, match: StageMatch) -> Path:
    sequence = scanner.completed.index(match.stage_id) + 1
    event_file = create_stage_event(
        request.log_file,
        sequence=sequence,
        run_id=request.run_id,
        thread_id=request.thread_id,
        command=request.command,
        stage_id=match.stage_id,
        matched_line=match.line,
        log_offset=match.log_offset,
    )
    _record_stage_runtime(request, scanner, {
        "type": "stage",
        "stage_id": match.stage_id,
        "event_file": str(event_file),
    })
    return event_file


def _record_stage_runtime(
    request: WorkerRequest,
    scanner: StageScanner,
    last_event: dict[str, object] | None,
) -> None:
    if request.runtime_file is None:
        return
    write_run_runtime(
        request.runtime_file,
        run_id=request.run_id,
        state="running",
        log_offset=scanner.offset,
        completed_stages=list(scanner.completed),
        last_event=last_event,
    )


def _handle_controls(
    request: WorkerRequest,
    process: subprocess.Popen[bytes],
    goal_guard: dict[str, object],
) -> str | None:
    controls = pending_controls(request.log_file)
    if not controls:
        return None
    command_file, command = controls[0]
    action = str(command["action"])
    try:
        if action == "stop":
            terminate_process_tree(process)
            result = "cancelled"
        else:
            _detach_goal(request, goal_guard)
            result = "detached"
        acknowledge_control(request.log_file, command_file, command, status=result)
        _record_terminal_runtime(request, result)
        return result
    except Exception as error:
        acknowledge_control(
            request.log_file,
            command_file,
            command,
            status="error",
            error=f"{type(error).__name__}: {error}",
        )
        return None


def _detach_goal(request: WorkerRequest, goal_guard: dict[str, object]) -> None:
    lease_id = goal_guard.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        return
    cancel_goal_guard(
        GoalGuardContext(thread_id=request.thread_id, codex_bin=request.codex_bin),
        run_id=request.run_id,
        lease_id=lease_id,
    )


def _record_terminal_runtime(request: WorkerRequest, state: str) -> None:
    if request.runtime_file is None:
        return
    write_run_runtime(
        request.runtime_file,
        run_id=request.run_id,
        state=state,
        last_event={"type": state},
    )
