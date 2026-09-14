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

import wake_run_core as wake_run
import wake_run_state as wake_state
import wake_run_worker as worker_runtime


def create_event(directory: Path, *, run_id: str = "run1") -> Path:
    return wake_state.create_completion_event(
        directory / f"{run_id}.log",
        run_id=run_id,
        thread_id="thread1",
        command="echo ok",
        exit_code=0,
        launch_error=None,
        wake_id=f"wake-{run_id}",
    )


def successful_queue(*args, before_attempt=None, **kwargs) -> int:
    if before_attempt:
        before_attempt(1)
    return 1


class RecoveryTests(unittest.TestCase):
    def test_stale_delivery_lock_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = create_event(Path(tmp))
            lock = event.with_suffix(".delivery.lock")
            wake_state.atomic_write_json(lock, {"pid": 999_999_999})
            with mock.patch.object(wake_state, "_process_is_alive", return_value=False):
                with wake_state.delivery_lock(event):
                    self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())

    def test_replay_reports_live_delivery_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            event = create_event(directory)
            with wake_state.delivery_lock(event):
                with mock.patch.object(wake_run, "preflight_codex_queue", return_value="/codex"):
                    result = wake_run.replay_pending(log_dir=directory, codex_bin="codex")
            self.assertEqual(result["status"], "replay_incomplete")
            self.assertEqual(result["busy"], 1)

    def test_malformed_completion_is_not_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            malformed = directory / "bad.completion.json"
            malformed.write_text("not json", encoding="utf-8")
            with mock.patch.object(wake_run, "preflight_codex_queue", return_value="/codex"):
                with self.assertRaises(json.JSONDecodeError):
                    wake_run.replay_pending(log_dir=directory, codex_bin="codex")

    def test_worker_creation_failure_is_persisted_and_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state_dir = directory / "state"
            with mock.patch.object(wake_run, "preflight_codex_queue", return_value="/codex"):
                with mock.patch.object(wake_run.subprocess, "Popen", side_effect=OSError("spawn denied")):
                    with self.assertRaisesRegex(RuntimeError, "spawn denied"):
                        wake_run.arm_watcher(
                            thread_id="thread",
                            command="echo no",
                            cwd=directory,
                            log_dir=state_dir,
                            codex_bin="codex",
                        )
            startup_files = list(state_dir.glob("*.startup.json"))
            self.assertEqual(len(startup_files), 1)
            self.assertEqual(wake_state.read_json(startup_files[0])["state"], "startup_failed")

    def test_wait_failure_after_startup_is_delivered_as_completion(self) -> None:
        process = mock.Mock(pid=321)
        process.wait.side_effect = OSError("wait failed")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            startup = directory / "run.startup.json"
            with mock.patch.object(worker_runtime.subprocess, "Popen", return_value=process):
                with mock.patch.object(wake_run, "queue_wakeup", side_effect=successful_queue):
                    result = wake_run.run_worker(
                        thread_id="thread",
                        command="echo started",
                        cwd=directory,
                        log_file=directory / "run.log",
                        codex_bin="codex",
                        run_id="run1",
                        startup_file=startup,
                    )
            event = wake_state.read_json(directory / "run.completion.json")
            self.assertEqual(result, wake_run.WORKER_STARTUP_FAILURE)
            self.assertEqual(wake_state.read_json(startup)["state"], "running")
            self.assertIn("wait failed", event["launch_error"])
            self.assertEqual(event["delivery"]["state"], wake_state.DELIVERY_DELIVERED)

    def test_nonfinite_queue_timeout_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"WAKE_RUN_QUEUE_TIMEOUT": "inf"}):
            with self.assertRaisesRegex(RuntimeError, "finite number"):
                wake_run.QueuePolicy.from_environment()

    def test_invalid_queue_policy_is_rejected_before_delivery(self) -> None:
        policy = wake_run.QueuePolicy(call_timeout=1, total_timeout=1, retry_delays=(-1,))
        with self.assertRaisesRegex(RuntimeError, "must not be negative"):
            wake_run.queue_wakeup("thread", "message", "codex", policy=policy)


class ReplayCliTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX executable integration test")
    def test_replay_cli_delivers_pending_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            event = create_event(directory)
            capture = directory / "capture.json"
            fake_codex = directory / "codex"
            fake_codex.write_text(
                f"#!{sys.executable}\nimport json,sys\n"
                f"open({str(capture)!r},'w').write(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            result = subprocess.run([
                sys.executable,
                str(SCRIPT),
                "--replay-pending",
                "--log-dir", str(directory),
                "--codex-bin", str(fake_codex),
            ], capture_output=True, text=True, check=False, timeout=10)
            response = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(response["delivered"], 1)
            self.assertEqual(
                wake_state.read_json(event)["delivery"]["state"], wake_state.DELIVERY_DELIVERED
            )


class WindowsQueueIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows PowerShell integration test")
    def test_codex_ps1_receives_multiline_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            capture = directory / "capture.json"
            fake_codex = directory / "codex.ps1"
            fake_codex.write_text(
                "$payload = ConvertTo-Json -Compress -InputObject @($args)\n"
                "$utf8 = New-Object System.Text.UTF8Encoding($false)\n"
                "[System.IO.File]::WriteAllText($env:WAKE_CAPTURE, $payload, $utf8)\n"
                "exit 0\n",
                encoding="utf-8",
            )
            message = "[后台任务唤醒通知]\n状态：执行完成\n注：系统后台唤醒"
            with mock.patch.dict(os.environ, {"WAKE_CAPTURE": str(capture)}):
                wake_run.queue_wakeup("thread-win", message, str(fake_codex))
            args = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(args[:4], ["queue", "--thread", "thread-win", "--message"])
            self.assertEqual(args[4], message)


if __name__ == "__main__":
    unittest.main()
