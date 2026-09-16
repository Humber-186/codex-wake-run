from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wake_run_adopt_worker as adopt_worker
import wake_run_control as control
import wake_run_events as events
import wake_run_goal as goal
import wake_run_goal_adopt as goal_adopt
import wake_run_registry as registry
import wake_run_stage_commit as stage_commit
import wake_run_state as state
import wake_run_supervisor as supervisor
from wake_run_models import WorkerRequest
from wake_run_stages import StageRule, StageScanner


class StageCommitTests(unittest.TestCase):
    def test_event_failure_does_not_advance_runtime_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log_file = directory / "run.log"
            runtime_file = directory / "run.runtime.json"
            log_file.write_text("DC DONE\nPLACE DONE\nCTS DONE\n", encoding="utf-8")
            registry.write_run_runtime(runtime_file, run_id="run1", state="running")
            scanner = StageScanner((
                StageRule("dc", "DC DONE"),
                StageRule("place", "PLACE DONE"),
                StageRule("cts", "CTS DONE"),
            ))
            committer = self._committer(log_file, runtime_file, scanner)
            original = stage_commit.create_stage_event
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("disk full")
                return original(*args, **kwargs)

            with mock.patch.object(stage_commit, "create_stage_event", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "disk full"):
                    committer.scan(final=True)
            runtime = state.read_json(runtime_file)
            completed, event_offset = events.persisted_stage_state(log_file)
            self.assertEqual(runtime["completed_stages"], [])
            self.assertEqual(runtime["log_offset"], 0)
            self.assertEqual(completed, ("dc",))

            resumed = StageScanner(scanner.rules, completed=completed, offset=event_offset)
            self._committer(log_file, runtime_file, resumed).scan(final=True)
            self.assertEqual(events.persisted_stage_state(log_file)[0], ("dc", "place", "cts"))

    def test_plain_offset_checkpoint_is_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log_file = directory / "run.log"
            runtime_file = directory / "run.runtime.json"
            log_file.write_text("noise\n", encoding="utf-8")
            registry.write_run_runtime(runtime_file, run_id="run1", state="running")
            scanner = StageScanner((StageRule("dc", "DC DONE"),))
            committer = self._committer(log_file, runtime_file, scanner)
            committer.scan()
            self.assertEqual(state.read_json(runtime_file)["log_offset"], 0)
            committer.scan(final=True)
            self.assertEqual(state.read_json(runtime_file)["log_offset"], len(b"noise\n"))

    @staticmethod
    def _committer(
        log_file: Path,
        runtime_file: Path,
        scanner: StageScanner,
    ) -> stage_commit.StageCommitter:
        context = stage_commit.StageCommitContext(
            run_id="run1",
            thread_id="thread1",
            command="flow",
            log_file=log_file,
            runtime_file=runtime_file,
        )
        return stage_commit.StageCommitter(context, scanner, lambda _path: None)


class SupervisorReliabilityTests(unittest.TestCase):
    def test_terminal_close_finishes_only_active_stage_delivery(self) -> None:
        started = threading.Event()
        release = threading.Event()
        delivered: list[str] = []

        def deliver(path: Path, _codex: str) -> None:
            started.set()
            release.wait(2)
            delivered.append(path.name)

        pump = supervisor.StageDeliveryPump(deliver, "codex")
        for name in ("one", "two", "three"):
            pump.submit(Path(name))
        self.assertTrue(started.wait(1))
        closer = threading.Thread(target=lambda: pump.close(wait=True, drain=False))
        closer.start()
        release.set()
        closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertEqual(delivered, ["one"])

    def test_terminal_observation_precedes_stage_delivery_drain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log_file = directory / "run.log"
            runtime_file = directory / "run.runtime.json"
            plan_file = directory / "stages.json"
            log_file.write_text("DONE\n", encoding="utf-8")
            plan_file.write_text(json.dumps({
                "schema_version": 1,
                "stages": [{"id": "done", "pattern": "DONE"}],
            }), encoding="utf-8")
            registry.write_run_runtime(runtime_file, run_id="run1", state="running")
            request = self._request(
                directory, log_file, runtime_file, plan_file=plan_file
            )
            release = threading.Event()

            outcome: list[tuple[int | None, str, dict[str, object]]] = []
            thread = threading.Thread(target=lambda: outcome.append(
                supervisor.supervise_owned_process(
                    request,
                    mock.Mock(),
                    initial_log_offset=0,
                    goal_guard={"lease_id": None},
                    deliver_stage=lambda _path, _codex: release.wait(2),
                    wait_process=lambda _process: 0,
                )
            ))
            thread.start()
            deadline = time.monotonic() + 1
            runtime = state.read_json(runtime_file)
            while runtime["state"] != "reviewing" and time.monotonic() < deadline:
                time.sleep(0.01)
                runtime = state.read_json(runtime_file)
            self.assertTrue(thread.is_alive())
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0][2]["pending"], 1)
        self.assertEqual(runtime["state"], "reviewing")
        self.assertEqual(runtime["exit_code"], 0)

    def test_stage_delivery_failure_is_returned_for_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log_file = directory / "run.log"
            runtime_file = directory / "run.runtime.json"
            plan_file = directory / "stages.json"
            log_file.write_text("DONE\n", encoding="utf-8")
            plan_file.write_text(json.dumps({
                "schema_version": 1,
                "stages": [{"id": "done", "pattern": "DONE"}],
            }), encoding="utf-8")
            registry.write_run_runtime(runtime_file, run_id="run1", state="running")
            request = self._request(
                directory, log_file, runtime_file, plan_file=plan_file
            )

            def fail_delivery(_path: Path, _codex: str) -> None:
                raise RuntimeError("queue down")

            result = supervisor.supervise_owned_process(
                request,
                mock.Mock(),
                initial_log_offset=0,
                goal_guard={"lease_id": None},
                deliver_stage=fail_delivery,
                wait_process=lambda _process: 0,
            )
        self.assertEqual(result[2]["pending"], 1)
        self.assertEqual(result[2]["failed"], 1)
        self.assertIn("queue down", str(result[2]["last_error"]))

    def test_detach_waits_for_already_submitted_stage_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log_file = directory / "run.log"
            runtime_file = directory / "run.runtime.json"
            registry.write_run_runtime(runtime_file, run_id="run1", state="running")
            request = self._request(directory, log_file, runtime_file, plan_file=None)
            command_dir, _ack_dir = control.control_paths(log_file)
            state.atomic_write_json(command_dir / "command.json", {
                "schema_version": 1,
                "command_id": "command",
                "run_id": "run1",
                "action": "detach",
                "created_at": "now",
            })
            release = threading.Event()
            event_file = events.create_stage_event(
                log_file,
                sequence=1,
                run_id="run1",
                thread_id="thread1",
                command="flow",
                stage_id="dc",
                matched_line="DC DONE",
                log_offset=8,
            )

            def deliver(path: Path, _codex: str) -> None:
                release.wait(2)
                state.update_delivery(
                    path, state=state.DELIVERY_DELIVERED, attempts=1, last_error=None
                )

            pump = supervisor.StageDeliveryPump(
                deliver, "codex"
            )
            pump.submit(event_file)
            timer = threading.Timer(0.1, release.set)
            timer.start()
            started = time.monotonic()
            result = supervisor._handle_controls(
                request, mock.Mock(), {"lease_id": None}, pump=pump
            )
            elapsed = time.monotonic() - started
            timer.join()
            ack = state.read_json(control.control_paths(log_file)[1] / "command.json")
        self.assertEqual(result, "detached")
        self.assertGreaterEqual(elapsed, 0.08)
        self.assertEqual(ack["stage_delivery"]["delivered"], 1)
        self.assertEqual(ack["stage_delivery"]["pending"], 0)

    @staticmethod
    def _request(
        directory: Path,
        log_file: Path,
        runtime_file: Path,
        *,
        plan_file: Path | None,
    ) -> WorkerRequest:
        return WorkerRequest(
            thread_id="thread1",
            command="flow",
            cwd=directory,
            log_file=log_file,
            codex_bin="codex",
            run_id="run1",
            startup_file=None,
            runtime_file=runtime_file,
            stage_plan_file=plan_file,
        )


class AdoptRaceTests(unittest.TestCase):
    def test_target_exit_after_gate_becomes_observed_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log_file = directory / "run.log"
            log_file.write_text("", encoding="utf-8")
            spec_file = directory / "run.spec.json"
            runtime_file = directory / "run.runtime.json"
            startup_file = directory / "attempt.startup.json"
            gate_file = directory / "attempt.gate.json"
            state.atomic_write_json(spec_file, {
                "run_id": "run1",
                "owner_thread_id": "thread1",
                "command": "flow",
                "log_file": str(log_file),
                "stage_plan_file": None,
            })
            state.atomic_write_json(runtime_file, {
                "run_id": "run1",
                "state": "running",
                "target_identity": {"pid": 123},
                "log_offset": 0,
            })
            state.atomic_write_json(gate_file, {
                "state": "committed",
                "run_id": "run1",
                "attempt_id": "attempt1",
                "goal_guard": {"lease_id": None},
            })
            with mock.patch.object(
                adopt_worker, "process_identity_matches", side_effect=[True, False]
            ):
                result = adopt_worker.run_adopted_worker(
                    spec_file=spec_file,
                    runtime_file=runtime_file,
                    startup_file=startup_file,
                    gate_file=gate_file,
                    launcher_pid=os.getpid(),
                    attempt_id="attempt1",
                    codex_bin="codex",
                    deliver_stage=mock.Mock(),
                    deliver_completion=mock.Mock(),
                )
            completion = state.read_json(log_file.with_suffix(".completion.json"))
        self.assertEqual(result, 0)
        self.assertEqual(completion["terminal_state"], "observed_exit")
        self.assertFalse(completion["exact_exit_code_available"])

    def test_gate_rejects_stale_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = Path(tmp) / "gate.json"
            state.atomic_write_json(gate, {
                "state": "committed",
                "run_id": "run1",
                "attempt_id": "old",
                "goal_guard": {},
            })
            with self.assertRaisesRegex(RuntimeError, "gate is invalid"):
                adopt_worker._await_gate(
                    gate, os.getpid(), "run1", attempt_id="new"
                )


class AdoptRollbackTests(unittest.TestCase):
    def test_goal_holder_transfer_can_be_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lease_root = Path(tmp)
            context = goal.GoalGuardContext(
                thread_id="thread1",
                codex_bin="codex",
                lease_root=lease_root,
            )
            lease_file = goal.goal_lease_path(context)
            state.atomic_write_json(lease_file, {
                "schema_version": 2,
                "lease_id": "lease1",
                "thread_id": "thread1",
                "original": {},
                "paused_snapshot": {},
                "holders": [{
                    "run_id": "run1",
                    "worker_pid": 111,
                    "completion_file": str(lease_root / "run.completion.json"),
                    "phase": "running",
                    "target_pid": 222,
                }],
                "phase": "orphaned",
                "updated_at": "now",
            })
            transfer = goal_adopt.adopt_goal_holder(
                context,
                run_id="run1",
                lease_id="lease1",
                worker_pid=333,
                target_pid=222,
            )
            goal_adopt.rollback_goal_holder_adoption(
                context,
                run_id="run1",
                lease_id="lease1",
                transfer=transfer,
            )
            lease = state.read_json(lease_file)
        self.assertEqual(lease["phase"], "orphaned")
        self.assertEqual(lease["holders"][0]["worker_pid"], 111)


class ControlAndRegistryTests(unittest.TestCase):
    def test_control_timeout_returns_durable_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            environment = {"CODEX_HOME": str(directory / "codex")}
            with mock.patch.dict(os.environ, environment):
                _spec, runtime = registry.create_run_record(
                    run_id="run1",
                    name=None,
                    thread_id="thread1",
                    command="flow",
                    cwd=directory,
                    log_file=directory / "run.log",
                    monitor={"enabled": False},
                    index_root=registry.run_index_root(),
                )
                registry.write_run_runtime(
                    runtime,
                    run_id="run1",
                    state="running",
                    worker_pid=os.getpid(),
                    target_pid=os.getpid(),
                )
                with mock.patch.object(control, "CONTROL_WAIT_SECONDS", 0.01):
                    result = control.issue_control("run1", "stop", thread_id="thread1")
                spec = state.read_json(directory / "run.spec.json")
                pending = control.pending_controls(Path(str(spec["log_file"])))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["action"], "stop")
        self.assertEqual(len(pending), 1)

    def test_unobserved_terminal_and_orphan_states_have_no_exact_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            environment = {"CODEX_HOME": str(directory / "codex")}
            with mock.patch.dict(os.environ, environment), mock.patch.object(
                registry, "process_is_alive", side_effect=lambda pid: pid in {12, 22}
            ):
                states = (("detached", 11, 12), ("running", 21, 22), ("running", 31, 32))
                views = []
                for index, (run_state, worker_pid, target_pid) in enumerate(states):
                    run_id = f"run{index}"
                    _spec, runtime = registry.create_run_record(
                        run_id=run_id,
                        name=None,
                        thread_id="thread1",
                        command="flow",
                        cwd=directory,
                        log_file=directory / f"{run_id}.log",
                        monitor={"enabled": False},
                        index_root=registry.run_index_root(),
                    )
                    registry.write_run_runtime(
                        runtime,
                        run_id=run_id,
                        state=run_state,
                        worker_pid=worker_pid,
                        target_pid=target_pid,
                        observer_mode="owned",
                    )
                    views.append(registry.show_run(run_id))
        self.assertEqual([view["state"] for view in views], ["detached", "orphaned", "lost"])
        self.assertEqual([view["exact_exit_code_available"] for view in views], [False] * 3)


class ScannerSemanticsTests(unittest.TestCase):
    def test_one_line_advances_at_most_one_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "run.log"
            log_file.write_text("DONE\n", encoding="utf-8")
            scanner = StageScanner((StageRule("a", "DONE"), StageRule("b", "DONE")))
            self.assertEqual([item.stage_id for item in scanner.scan(log_file)], ["a"])
            self.assertEqual(scanner.completed, ["a"])


if __name__ == "__main__":
    unittest.main()
