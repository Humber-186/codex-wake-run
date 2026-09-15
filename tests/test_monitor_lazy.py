from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wake_run_core as core
import wake_run_state as state
import wake_run_worker as worker


class LazyMonitorWorkerTests(unittest.TestCase):
    def test_skipped_success_is_persisted_and_visible_in_wake_message(self) -> None:
        result = worker.ExecutionResult(
            exit_code=0,
            error=None,
            startup_confirmed=True,
            duration_seconds=1.0,
            user_seconds=0.1,
            system_seconds=0.2,
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            request = worker.WorkerRequest(
                "thread",
                "command",
                directory,
                directory / "run.log",
                "codex",
                "run1",
                None,
                directory / "run.monitor.json",
            )
            delivered: list[Path] = []
            with mock.patch.object(worker, "_execute", return_value=result):
                exit_code = worker.run_worker(
                    request,
                    deliver=lambda path, _codex: delivered.append(path),
                    notify_state_failure=lambda _failure: 74,
                    triage=lambda *_args: None,
                )
            event = state.read_json(delivered[0])
        self.assertEqual(exit_code, 0)
        self.assertEqual(event["monitor"]["status"], "skipped_success")
        self.assertIn("monitor_status: skipped_success", core._event_message(event))

    def test_success_after_reviewed_retry_does_not_reuse_failure_summary(self) -> None:
        results = iter([
            worker.ExecutionResult(7, None, True, 1.0, 0.1, 0.1),
            worker.ExecutionResult(0, None, True, 1.0, 0.1, 0.1),
        ])
        decisions = iter([
            worker.TriageDecision(
                "retry_exact",
                "temporary service failure",
                "retry is authorized",
                "transient_external",
                "gpt-5.6-luna",
                "session",
                "hash",
            ),
            None,
        ])
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            request = worker.WorkerRequest(
                "thread", "command", directory, directory / "run.log",
                "codex", "run1", None, directory / "run.monitor.json",
            )
            delivered: list[Path] = []
            with mock.patch.object(worker, "_execute", side_effect=results):
                worker.run_worker(
                    request,
                    deliver=lambda path, _codex: delivered.append(path),
                    notify_state_failure=lambda _failure: 74,
                    triage=lambda *_args: next(decisions),
                )
            event = state.read_json(delivered[0])
        message = core._event_message(event)
        self.assertIn("monitor_status: skipped_success", message)
        self.assertNotIn("temporary service failure", message)


if __name__ == "__main__":
    unittest.main()
