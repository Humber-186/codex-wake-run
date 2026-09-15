from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wake_run_process as process_runtime

LONG_SLEEP_SECONDS = 30
PID_WAIT_TIMEOUT = 2
CHECK_INTERVAL = 0.01


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ProcessTreeTests(unittest.TestCase):
    def test_target_gets_an_independent_process_group(self) -> None:
        self.assertEqual(
            process_runtime.target_popen_kwargs(platform="posix"),
            {"start_new_session": True},
        )
        with mock.patch.object(process_runtime.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True):
            self.assertEqual(
                process_runtime.target_popen_kwargs(platform="nt"),
                {"creationflags": 512},
            )

    def test_posix_tree_escalates_from_term_to_kill(self) -> None:
        process = mock.Mock(pid=321)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("target", 1), 0]
        if os.name == "nt":
            self.skipTest("POSIX process-group test")
        with mock.patch.object(process_runtime.os, "killpg", create=True) as kill_group:
            process_runtime.terminate_process_tree(process, platform="posix", timeout=1)
        self.assertEqual(kill_group.call_args_list, [
            mock.call(321, signal.SIGTERM),
            mock.call(321, signal.SIGKILL),
        ])

    def test_windows_tree_uses_taskkill_tree_flag(self) -> None:
        process = mock.Mock(pid=654)
        process.poll.return_value = None
        completed = subprocess.CompletedProcess(["taskkill"], 0)
        with mock.patch.object(process_runtime.subprocess, "run", return_value=completed) as run:
            process_runtime.terminate_process_tree(process, platform="nt", timeout=1)
        invocation = run.call_args.args[0]
        self.assertEqual(invocation[:3], ["taskkill", "/PID", "654"])
        self.assertIn("/T", invocation)
        self.assertIn("/F", invocation)
        process.wait.assert_called_once_with(timeout=1)

    @unittest.skipIf(os.name == "nt", "POSIX process-group integration test")
    def test_real_posix_tree_termination_reaches_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            source = (
                "import subprocess,sys; from pathlib import Path; "
                f"child=subprocess.Popen([sys.executable,'-c','import time; time.sleep({LONG_SLEEP_SECONDS})']); "
                f"Path({str(pid_file)!r}).write_text(str(child.pid)); child.wait()"
            )
            parent = subprocess.Popen(
                [sys.executable, "-c", source],
                **process_runtime.target_popen_kwargs(),
            )
            try:
                deadline = time.monotonic() + PID_WAIT_TIMEOUT
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(CHECK_INTERVAL)
                self.assertTrue(pid_file.exists(), "descendant PID was not reported")
                child_pid = int(pid_file.read_text())
                process_runtime.terminate_process_tree(parent)
                deadline = time.monotonic() + PID_WAIT_TIMEOUT
                while process_exists(child_pid) and time.monotonic() < deadline:
                    time.sleep(CHECK_INTERVAL)
                self.assertFalse(process_exists(child_pid), f"descendant {child_pid} survived cleanup")
            finally:
                try:
                    os.killpg(parent.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                parent.wait()


if __name__ == "__main__":
    unittest.main()
