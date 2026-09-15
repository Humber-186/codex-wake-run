from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SCRIPT = SCRIPTS / "wake_run.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wake_run_registry as registry
import wake_run_state as state


class RunRegistryTests(unittest.TestCase):
    def create_run(self, directory: Path, *, run_id: str = "run1") -> tuple[Path, Path]:
        log_file = directory / "state" / f"{run_id}.log"
        return registry.create_run_record(
            run_id=run_id,
            name="route",
            thread_id="thread1",
            command="sleep 10",
            cwd=directory,
            log_file=log_file,
            monitor={"enabled": False},
            index_root=registry.run_index_root(),
        )

    def test_running_run_is_globally_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(directory / "codex")}), mock.patch.object(
                registry, "process_is_alive", return_value=True
            ):
                _spec, runtime = self.create_run(directory)
                registry.write_run_runtime(
                    runtime,
                    run_id="run1",
                    state="running",
                    worker_pid=101,
                    target_pid=102,
                )
                result = registry.list_runs(thread_id="thread1", active_only=True)
        self.assertEqual(result["failures"], {})
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["runs"][0]["name"], "route")
        self.assertEqual(result["runs"][0]["health"], "healthy")

    def test_completion_overrides_stale_running_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(directory / "codex")}):
                spec_file, runtime = self.create_run(directory)
                spec = state.read_json(spec_file)
                registry.write_run_runtime(
                    runtime,
                    run_id="run1",
                    state="running",
                    worker_pid=os.getpid(),
                    target_pid=os.getpid(),
                )
                state.create_completion_event(
                    Path(str(spec["log_file"])),
                    run_id="run1",
                    thread_id="thread1",
                    command="sleep 10",
                    exit_code=0,
                    launch_error=None,
                    wake_id="wake1",
                )
                run = registry.show_run("run1")
        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["exit_code"], 0)
        self.assertIsNotNone(run["delivery"])

    def test_bad_index_is_reported_without_hiding_valid_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(directory / "codex")}):
                self.create_run(directory)
                bad = registry.run_index_root() / "bad.json"
                bad.write_text("not json", encoding="utf-8")
                result = registry.list_runs(thread_id="thread1", active_only=False)
        self.assertEqual(len(result["runs"]), 1)
        self.assertIn("bad.json", result["failures"])

    def test_cli_lists_and_shows_persisted_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            environment = {
                **os.environ,
                "CODEX_HOME": str(directory / "codex"),
                "CODEX_THREAD_ID": "thread1",
            }
            with mock.patch.dict(os.environ, environment):
                self.create_run(directory)
            listed = subprocess.run(
                [sys.executable, str(SCRIPT), "--list", "--active"],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=10,
            )
            shown = subprocess.run(
                [sys.executable, str(SCRIPT), "--show", "run1"],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=10,
            )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["runs"][0]["run_id"], "run1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["name"], "route")


if __name__ == "__main__":
    unittest.main()
