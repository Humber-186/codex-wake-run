"""Recovery observer for a target whose original worker was lost."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Callable

from wake_run_control import acknowledge_control, pending_controls
from wake_run_events import persisted_stage_state, stage_delivery_summary
from wake_run_goal import GoalGuardContext, cancel_goal_guard
from wake_run_process import process_identity_matches, terminate_identified_process_tree
from wake_run_registry import write_run_runtime
from wake_run_stage_commit import StageCommitContext, StageCommitter
from wake_run_stages import StageScanner, load_stage_plan
from wake_run_state import (
    atomic_write_json,
    create_completion_event,
    process_is_alive,
    read_json,
    update_stage_delivery,
    utc_now,
)
from wake_run_supervisor import SUPERVISOR_INTERVAL_SECONDS, StageDeliveryPump

EventDelivery = Callable[[Path, str], None]


def run_adopted_worker(
    *,
    spec_file: Path,
    runtime_file: Path,
    startup_file: Path,
    gate_file: Path,
    launcher_pid: int,
    attempt_id: str,
    codex_bin: str,
    deliver_stage: EventDelivery,
    deliver_completion: EventDelivery,
) -> int:
    spec = read_json(spec_file)
    runtime = read_json(runtime_file)
    run_id = _required_text(spec.get("run_id"), "run_id")
    identity = _identity(runtime)
    if not process_identity_matches(identity):
        raise RuntimeError("Target process identity changed before adopt preparation")
    _write_status(startup_file, state="prepared", run_id=run_id, attempt_id=attempt_id)
    goal_guard = _await_gate(
        gate_file, launcher_pid, run_id, attempt_id=attempt_id
    )
    log_file = Path(_required_text(spec.get("log_file"), "log_file"))
    write_run_runtime(
        runtime_file,
        run_id=run_id,
        state="running",
        worker_pid=os.getpid(),
        observer_mode="adopted",
        last_event={"type": "adopted"},
    )
    _write_status(
        startup_file,
        state="running",
        run_id=run_id,
        attempt_id=attempt_id,
        process_pid=int(identity["pid"]),
    )
    return _observe(
        spec,
        runtime_file=runtime_file,
        goal_guard=goal_guard,
        identity=identity,
        codex_bin=codex_bin,
        deliver_stage=deliver_stage,
        deliver_completion=deliver_completion,
    )


def _observe(
    spec: dict[str, object],
    *,
    runtime_file: Path,
    goal_guard: dict[str, object],
    identity: dict[str, object],
    codex_bin: str,
    deliver_stage: EventDelivery,
    deliver_completion: EventDelivery,
) -> int:
    log_file = Path(_required_text(spec.get("log_file"), "log_file"))
    scanner = _scanner(spec, runtime_file, log_file)
    pump = StageDeliveryPump(deliver_stage, codex_bin)
    committer = StageCommitter(
        StageCommitContext(
            run_id=str(spec["run_id"]),
            thread_id=str(spec["owner_thread_id"]),
            command=str(spec["command"]),
            log_file=log_file,
            runtime_file=runtime_file,
        ),
        scanner,
        pump.submit,
    )
    terminal_state = "observed_exit"
    while process_identity_matches(identity):
        committer.scan(final=False)
        action = _handle_control(
            spec,
            runtime_file,
            log_file,
            identity=identity,
            goal_guard=goal_guard,
            codex_bin=codex_bin,
            pump=pump,
        )
        if action == "detached":
            return 0
        if action == "cancelled":
            terminal_state = action
            break
        time.sleep(SUPERVISOR_INTERVAL_SECONDS)
    committer.scan(final=True)
    stage_delivery = stage_delivery_summary(log_file)
    event = _persist_terminal(
        spec,
        log_file,
        goal_guard,
        terminal_state=terminal_state,
        stage_delivery=stage_delivery,
    )
    write_run_runtime(
        runtime_file,
        run_id=str(spec["run_id"]),
        state=terminal_state,
        worker_pid=os.getpid(),
        completion_file=event,
        observer_mode="adopted",
        last_event={"type": terminal_state, "event_file": str(event)},
    )
    pump.close(wait=True, drain=False)
    update_stage_delivery(
        event, stage_delivery_summary(log_file, delivery_errors=pump.errors)
    )
    deliver_completion(event, codex_bin)
    return 0


def _scanner(spec: dict[str, object], runtime_file: Path, log_file: Path) -> StageScanner | None:
    plan = spec.get("stage_plan_file")
    if not isinstance(plan, str):
        return None
    runtime = read_json(runtime_file)
    offset = runtime.get("log_offset", 0)
    if not isinstance(offset, int) or offset < 0:
        raise RuntimeError("Run has an invalid stage log offset")
    completed, event_offset = persisted_stage_state(log_file)
    return StageScanner(
        load_stage_plan(Path(plan)),
        completed=completed,
        offset=max(offset, event_offset),
    )


def _handle_control(
    spec: dict[str, object],
    runtime_file: Path,
    log_file: Path,
    *,
    identity: dict[str, object],
    goal_guard: dict[str, object],
    codex_bin: str,
    pump: StageDeliveryPump,
) -> str | None:
    controls = pending_controls(log_file)
    if not controls:
        return None
    path, command = controls[0]
    action = str(command["action"])
    try:
        if action == "stop":
            terminate_identified_process_tree(identity)
            state = "cancelled"
        else:
            pump.close(wait=True)
            _release_detached_goal(spec, goal_guard, codex_bin)
            state = "detached"
        delivery = (
            stage_delivery_summary(log_file, delivery_errors=pump.errors)
            if state == "detached"
            else None
        )
        acknowledge_control(
            log_file,
            path,
            command,
            status=state,
            stage_delivery=delivery,
        )
        write_run_runtime(runtime_file, run_id=str(spec["run_id"]), state=state)
        return state
    except Exception as error:
        acknowledge_control(
            log_file, path, command, status="error", error=f"{type(error).__name__}: {error}"
        )
        return None


def _release_detached_goal(
    spec: dict[str, object], goal_guard: dict[str, object], codex_bin: str
) -> None:
    lease_id = goal_guard.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        return
    cancel_goal_guard(
        GoalGuardContext(thread_id=str(spec["owner_thread_id"]), codex_bin=codex_bin),
        run_id=str(spec["run_id"]),
        lease_id=lease_id,
    )


def _persist_terminal(
    spec: dict[str, object],
    log_file: Path,
    goal_guard: dict[str, object],
    *,
    terminal_state: str,
    stage_delivery: dict[str, object],
) -> Path:
    return create_completion_event(
        log_file,
        run_id=str(spec["run_id"]),
        thread_id=str(spec["owner_thread_id"]),
        command=str(spec["command"]),
        exit_code=None,
        launch_error=None,
        wake_id=uuid.uuid4().hex,
        monitor_status="disabled_after_adopt",
        goal_guard=goal_guard,
        terminal_state=terminal_state,
        observer_mode="adopted",
        stage_delivery=stage_delivery,
    )


def _await_gate(
    path: Path,
    launcher_pid: int,
    run_id: str,
    *,
    attempt_id: str,
) -> dict[str, object]:
    while not path.exists():
        if not process_is_alive(launcher_pid):
            raise RuntimeError("Adopt launcher exited before committing the gate")
        time.sleep(0.05)
    gate = read_json(path)
    if (
        gate.get("state") != "committed"
        or gate.get("run_id") != run_id
        or gate.get("attempt_id") != attempt_id
    ):
        raise RuntimeError("Adopt commit gate is invalid")
    guard = gate.get("goal_guard")
    if not isinstance(guard, dict):
        raise RuntimeError("Adopt commit gate has no Goal guard")
    return guard


def _write_status(
    path: Path,
    *,
    state: str,
    run_id: str,
    attempt_id: str,
    process_pid: int | None = None,
) -> None:
    atomic_write_json(path, {
        "schema_version": 2,
        "state": state,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "worker_pid": os.getpid(),
        "process_pid": process_pid,
        "updated_at": utc_now(),
    })


def _identity(runtime: dict[str, object]) -> dict[str, object]:
    identity = runtime.get("target_identity")
    if not isinstance(identity, dict):
        raise RuntimeError("Run has no Linux target identity and cannot be adopted")
    return identity


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Run record has no valid {field}")
    return value
