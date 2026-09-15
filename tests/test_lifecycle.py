from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wake_run_adopt as adopt
import wake_run_control as control
import wake_run_core as core
import wake_run_events as events
import wake_run_goal_adopt as goal_adopt
import wake_run_goal as goal
import wake_run_launcher as launcher
import wake_run_registry as registry
import wake_run_state as state
from wake_run_stages import StageScanner, load_stage_plan

WAIT_SECONDS = 8
POLL_SECONDS = 0.02


def python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


class StagePlanTests(unittest.TestCase):
    def test_ordered_scanner_ignores_later_stage_until_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plan = directory / "stages.json"
            log = directory / "run.log"
            plan.write_text(json.dumps({
                "schema_version": 1,
                "stages": [
                    {"id": "dc", "pattern": "DC DONE"},
                    {"id": "place", "pattern": "PLACE DONE"},
                ],
            }), encoding="utf-8")
            log.write_text("PLACE DONE\nDC DONE\nPLACE DONE\n", encoding="utf-8")
            scanner = StageScanner(load_stage_plan(plan))
            matches = scanner.scan(log, final=True)
        self.assertEqual([match.stage_id for match in matches], ["dc", "place"])

    def test_invalid_regex_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "stages.json"
            plan.write_text(json.dumps({
                "schema_version": 1,
                "stages": [{"id": "dc", "pattern": "["}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid regex"):
                load_stage_plan(plan)

    def test_partial_log_line_is_not_matched_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plan = directory / "stages.json"
            log = directory / "run.log"
            plan.write_text(json.dumps({
                "schema_version": 1,
                "stages": [{"id": "dc", "pattern": "DC DONE"}],
            }), encoding="utf-8")
            log.write_text("DC DO", encoding="utf-8")
            scanner = StageScanner(load_stage_plan(plan))
            self.assertEqual(scanner.scan(log), ())
            with log.open("a", encoding="utf-8") as stream:
                stream.write("NE\n")
            self.assertEqual([item.stage_id for item in scanner.scan(log)], ["dc"])

    def test_replay_delivers_persisted_stage_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            event = events.create_stage_event(
                directory / "run.log",
                sequence=1,
                run_id="run1",
                thread_id="thread1",
                command="flow",
                stage_id="dc",
                matched_line="DC DONE",
                log_offset=8,
            )
            with mock.patch.object(core, "preflight_codex_queue", return_value="/codex"):
                with mock.patch.object(core, "queue_wakeup", return_value=1) as queue_wakeup:
                    result = core.replay_pending(log_dir=directory, codex_bin="codex")
            delivery_state = state.read_json(event)["delivery"]["state"]
            message = queue_wakeup.call_args.args[1]
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(delivery_state, "delivered")
        self.assertIn("[后台任务阶段-系统提示]", message)


class GoalAdoptTests(unittest.TestCase):
    def test_orphaned_holder_moves_to_recovery_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lease_root = Path(tmp)
            context = goal.GoalGuardContext(
                thread_id="thread1",
                codex_bin="codex",
                lease_root=lease_root,
            )
            path = goal.goal_lease_path(context)
            state.atomic_write_json(path, {
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
            goal_adopt.adopt_goal_holder(
                context,
                run_id="run1",
                lease_id="lease1",
                worker_pid=333,
                target_pid=222,
            )
            lease = state.read_json(path)
        self.assertEqual(lease["phase"], "paused")
        self.assertEqual(lease["holders"][0]["worker_pid"], 333)


@unittest.skipIf(os.name == "nt", "POSIX lifecycle integration tests")
class LifecycleIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.codex_home = self.directory / "codex-home"
        self.capture = self.directory / "queue.jsonl"
        self.fake_codex = self.directory / "codex"
        self.fake_codex.write_text(
            f"#!{sys.executable}\nimport json,sys\n"
            f"open({str(self.capture)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\n",
            encoding="utf-8",
        )
        self.fake_codex.chmod(0o755)
        self.environment = {"CODEX_HOME": str(self.codex_home)}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def launch(self, command: str, *, stage_plan: Path | None = None) -> dict[str, object]:
        with warnings.catch_warnings(), mock.patch.dict(os.environ, self.environment):
            warnings.simplefilter("ignore", ResourceWarning)
            return launcher.arm_watcher(
                thread_id="thread1",
                command=command,
                cwd=self.directory,
                log_dir=self.directory / "state",
                codex_bin=str(self.fake_codex),
                goal_policy="ignore",
                index_root=registry.run_index_root(),
                stage_plan_source=stage_plan,
            )

    def wait_for_state(self, run_id: str, expected: str) -> dict[str, object]:
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            with mock.patch.dict(os.environ, self.environment):
                run = registry.show_run(run_id)
            if run["state"] == expected:
                return run
            time.sleep(POLL_SECONDS)
        self.fail(f"run {run_id} did not reach {expected}")

    def test_two_stages_wake_before_terminal_event(self) -> None:
        plan = self.directory / "stages.json"
        plan.write_text(json.dumps({
            "schema_version": 1,
            "stages": [
                {"id": "dc", "pattern": "DC DONE"},
                {"id": "route", "pattern": "ROUTE DONE"},
            ],
        }), encoding="utf-8")
        command = python_command(
            "import time; print('DC DONE', flush=True); time.sleep(.3); "
            "print('ROUTE DONE', flush=True); time.sleep(.3)"
        )
        armed = self.launch(command, stage_plan=plan)
        run = self.wait_for_state(str(armed["run_id"]), "completed")
        events = sorted((self.directory / "state").glob("*.event.json"))
        self.assertEqual(run["completed_stages"], ["dc", "route"])
        self.assertEqual([state.read_json(path)["stage_id"] for path in events], ["dc", "route"])
        completion = Path(str(armed["log_file"])).with_suffix(".completion.json")
        deadline = time.monotonic() + WAIT_SECONDS
        while state.read_json(completion)["delivery"]["state"] != "delivered":
            if time.monotonic() >= deadline:
                self.fail("terminal event was not delivered")
            time.sleep(POLL_SECONDS)
        messages = [json.loads(line)[4] for line in self.capture.read_text().splitlines() if "--message" in line]
        self.assertEqual(sum("[后台任务阶段-系统提示]" in item for item in messages), 2)
        self.assertEqual(sum("[后台任务完成-系统提示]" in item for item in messages), 1)

    def test_stop_cancels_target_and_wakes_terminal(self) -> None:
        armed = self.launch(python_command("import time; time.sleep(30)"))
        with mock.patch.dict(os.environ, self.environment):
            ack = control.issue_control(str(armed["run_id"]), "stop", thread_id="thread1")
        self.assertEqual(ack["status"], "cancelled")
        run = self.wait_for_state(str(armed["run_id"]), "cancelled")
        self.assertFalse(run["target_alive"])

    def test_detach_leaves_target_alive_without_terminal_event(self) -> None:
        armed = self.launch(python_command("import time; time.sleep(1)"))
        with mock.patch.dict(os.environ, self.environment):
            ack = control.issue_control(str(armed["run_id"]), "detach", thread_id="thread1")
        self.assertEqual(ack["status"], "detached")
        run = self.wait_for_state(str(armed["run_id"]), "detached")
        self.assertTrue(run["target_alive"])
        self.assertFalse(Path(str(armed["log_file"])).with_suffix(".completion.json").exists())

    def test_adopt_observes_orphan_with_explicit_unknown_exit_code(self) -> None:
        armed = self.launch(python_command("import time; time.sleep(1.2)"))
        os.kill(int(armed["worker_pid"]), signal.SIGKILL)
        deadline = time.monotonic() + WAIT_SECONDS
        while state.process_is_alive(int(armed["worker_pid"])) and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        with warnings.catch_warnings(), mock.patch.dict(os.environ, self.environment):
            warnings.simplefilter("ignore", ResourceWarning)
            adopted = adopt.adopt_run(
                str(armed["run_id"]), thread_id="thread1", codex_bin=str(self.fake_codex)
            )
        self.assertEqual(adopted["status"], "adopted")
        run = self.wait_for_state(str(armed["run_id"]), "observed_exit")
        self.assertIsNone(run["exit_code"])
        self.assertFalse(run["exact_exit_code_available"])


if __name__ == "__main__":
    unittest.main()
