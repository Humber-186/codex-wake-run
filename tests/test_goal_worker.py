from __future__ import annotations

import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wake_run_state as state
import wake_run_worker as worker


def request(directory: Path) -> worker.WorkerRequest:
    return worker.WorkerRequest(
        thread_id="thread1",
        command="echo ok",
        cwd=directory,
        log_file=directory / "run.log",
        codex_bin="codex",
        run_id="run1",
        startup_file=directory / "run.startup.json",
    )


PAUSED_GUARD = {"mode": "paused", "verified": True, "lease_id": "lease1"}


class GoalWorkerTests(unittest.TestCase):
    def test_spawning_is_persisted_before_popen_and_running_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            order: list[str] = []
            process = mock.Mock(pid=321)
            process.poll.return_value = None
            with mock.patch.object(
                worker, "mark_goal_holder_spawning", side_effect=lambda *_args, **_kwargs: order.append("spawning")
            ), mock.patch.object(
                worker.subprocess, "Popen", side_effect=lambda *_args, **_kwargs: (order.append("popen"), process)[1]
            ), mock.patch.object(
                worker, "mark_goal_holder_running", side_effect=lambda *_args, **_kwargs: order.append("running")
            ):
                result, error, confirmed = worker._start_process(
                    request(Path(tmp)),
                    io.BytesIO(),
                    mock.Mock(),
                    confirm_startup=False,
                    goal_guard=PAUSED_GUARD,
                )
            self.assertEqual(order, ["spawning", "popen", "running"])
            self.assertIs(result, process)
            self.assertIsNone(error)
            self.assertTrue(confirmed)

    def test_spawning_marker_failure_prevents_popen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                worker,
                "mark_goal_holder_spawning",
                side_effect=RuntimeError("lease missing"),
            ), mock.patch.object(worker.subprocess, "Popen") as popen:
                result, error, confirmed = worker._start_process(
                    request(Path(tmp)),
                    io.BytesIO(),
                    mock.Mock(),
                    confirm_startup=True,
                    goal_guard=PAUSED_GUARD,
                )
            popen.assert_not_called()
            self.assertIsNone(result)
            self.assertIn("lease missing", str(error))
            self.assertFalse(confirmed)

    def test_running_marker_failure_terminates_spawned_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            process = mock.Mock(pid=321)
            with mock.patch.object(worker, "mark_goal_holder_spawning"), mock.patch.object(
                worker.subprocess, "Popen", return_value=process
            ), mock.patch.object(
                worker, "mark_goal_holder_running", side_effect=RuntimeError("write failed")
            ), mock.patch.object(worker, "terminate_process_tree") as terminate:
                result, error, confirmed = worker._start_process(
                    request(Path(tmp)),
                    io.BytesIO(),
                    mock.Mock(),
                    confirm_startup=True,
                    goal_guard=PAUSED_GUARD,
                )
            terminate.assert_called_once_with(process)
            self.assertIsNone(result)
            self.assertIn("write failed", str(error))
            self.assertFalse(confirmed)


class GoalLockTests(unittest.TestCase):
    def test_blocking_process_lock_waits_for_current_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "goal.lock"
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()

            def hold_first() -> None:
                with state.process_lock(lock_path, "Goal lease", blocking=True):
                    first_entered.set()
                    release_first.wait(timeout=1)

            def hold_second() -> None:
                with state.process_lock(lock_path, "Goal lease", blocking=True):
                    second_entered.set()

            first = threading.Thread(target=hold_first)
            second = threading.Thread(target=hold_second)
            first.start()
            self.assertTrue(first_entered.wait(timeout=1))
            second.start()
            self.assertFalse(second_entered.wait(timeout=0.05))
            release_first.set()
            self.assertTrue(second_entered.wait(timeout=1))
            first.join(timeout=1)
            second.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
