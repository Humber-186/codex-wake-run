from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SCRIPT = SCRIPTS / "wake_run.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wake_run_core as wake_run
import wake_run_platform as wake_platform
import wake_run_state as wake_state
import wake_run_worker as worker_runtime


def python_shell_command(source: str) -> str:
    if os.name == "nt":
        executable = str(Path(sys.executable)).replace("'", "''")
        code = source.replace("'", "''")
        return f"& '{executable}' -c '{code}'"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def successful_queue(*args, before_attempt=None, **kwargs) -> int:
    if before_attempt:
        before_attempt(1)
    return 1


def acquire_delivery_lock(event: Path) -> None:
    with wake_state.delivery_lock(event):
        pass


class InvocationTests(unittest.TestCase):
    def test_windows_experiment_preserves_exit_code(self) -> None:
        invocation = wake_platform.build_experiment_invocation(
            "& '.\\experiment.ps1'",
            platform="nt",
            powershell_bin=r"C:\Program Files\PowerShell\7\pwsh.exe",
        )
        self.assertEqual(invocation[0], r"C:\Program Files\PowerShell\7\pwsh.exe")
        self.assertIn("$LASTEXITCODE", invocation[-1])
        self.assertNotIn("cmd.exe", " ".join(invocation).lower())

    def test_windows_codex_ps1_uses_powershell(self) -> None:
        invocation = wake_platform.build_codex_invocation(
            r"C:\npm\codex.ps1",
            ["queue", "--message", "hello\nworld"],
            platform="nt",
            powershell_bin="pwsh.exe",
        )
        self.assertEqual(invocation[:6], [
            "pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", r"C:\npm\codex.ps1",
        ])
        self.assertEqual(invocation[-1], "hello\nworld")

    @mock.patch.object(wake_platform.shutil, "which")
    def test_windows_codex_resolution_prefers_cmd(self, which: mock.Mock) -> None:
        candidates = {
            "codex.exe": None,
            "codex.cmd": r"C:\npm\codex.cmd",
            "codex.bat": None,
            "codex.ps1": r"C:\npm\codex.ps1",
            "codex": r"C:\npm\codex.ps1",
        }
        which.side_effect = lambda name: candidates.get(name)
        self.assertEqual(wake_platform.resolve_codex_executable("codex", platform="nt"), r"C:\npm\codex.cmd")

    @unittest.skipIf(os.name == "nt", "POSIX bash test")
    def test_posix_uses_bash_pipefail(self) -> None:
        invocation = wake_platform.build_experiment_invocation("echo hi", platform="posix")
        if wake_platform.shutil.which("bash"):
            self.assertIn("pipefail", invocation)


class QueueTests(unittest.TestCase):
    @mock.patch.object(wake_run, "resolve_codex_executable", return_value="/usr/bin/codex")
    @mock.patch.object(wake_run, "_attempt_queue")
    def test_queue_retries_then_returns_attempt_count(self, attempt: mock.Mock, _resolve: mock.Mock) -> None:
        attempt.side_effect = [RuntimeError("transient"), None]
        policy = wake_run.QueuePolicy(call_timeout=1, total_timeout=1, retry_delays=(0,))
        self.assertEqual(wake_run.queue_wakeup("thread", "message", "codex", policy=policy), 2)

    @mock.patch.object(wake_run, "resolve_codex_executable", return_value="/usr/bin/codex")
    @mock.patch.object(wake_run, "_attempt_queue")
    def test_attempt_timeout_is_bounded_by_remaining_total(self, attempt: mock.Mock, _resolve: mock.Mock) -> None:
        policy = wake_run.QueuePolicy(call_timeout=30, total_timeout=5, retry_delays=(1,))
        with mock.patch.object(wake_run.time, "monotonic", side_effect=[0, 3]):
            wake_run.queue_wakeup("thread", "message", "codex", policy=policy)
        self.assertEqual(attempt.call_args.kwargs["timeout"], 2)

    @mock.patch.object(wake_run, "resolve_codex_executable", return_value="/usr/bin/codex")
    @mock.patch.object(wake_run, "_attempt_queue", side_effect=RuntimeError("down"))
    def test_total_timeout_surfaces_last_error(self, _attempt: mock.Mock, _resolve: mock.Mock) -> None:
        policy = wake_run.QueuePolicy(call_timeout=1, total_timeout=0.01, retry_delays=(0.01,))
        with self.assertRaisesRegex(RuntimeError, "Last error.*down"):
            wake_run.queue_wakeup("thread", "message", "codex", policy=policy)

    @mock.patch.object(wake_run, "resolve_codex_executable", return_value="/usr/bin/codex")
    @mock.patch.object(wake_run.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 30))
    def test_preflight_timeout_is_explicit(self, _run: mock.Mock, _resolve: mock.Mock) -> None:
        with self.assertRaisesRegex(RuntimeError, "preflight timed out"):
            wake_run.preflight_codex_queue("codex")

class CompletionStateTests(unittest.TestCase):
    def create_event(self, directory: Path) -> Path:
        return wake_state.create_completion_event(
            directory / "run.log",
            run_id="run1",
            thread_id="thread1",
            command="echo ok",
            exit_code=0,
            launch_error=None,
            wake_id="wake1",
        )

    def test_completion_lifecycle_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.create_event(Path(tmp))
            initial = wake_state.read_json(path)
            self.assertEqual(initial["delivery"]["state"], wake_state.DELIVERY_PENDING)
            wake_state.update_delivery(
                path,
                state=wake_state.DELIVERY_DELIVERED,
                attempts=2,
                last_error=None,
            )
            delivered = wake_state.read_json(path)
            self.assertEqual(delivered["delivery"]["state"], wake_state.DELIVERY_DELIVERED)
            self.assertEqual(delivered["delivery"]["attempts"], 2)
            self.assertIsNotNone(delivered["delivery"]["delivered_at"])
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_live_delivery_lock_is_not_stolen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = self.create_event(Path(tmp))
            with wake_state.delivery_lock(event):
                self.assertRaises(wake_state.DeliveryInProgressError, acquire_delivery_lock, event)


class WorkerTests(unittest.TestCase):
    def run_with_successful_delivery(self, *, command: str, directory: Path, startup: bool = False) -> int:
        with mock.patch.object(wake_run, "queue_wakeup", side_effect=successful_queue):
            return wake_run.run_worker(
                thread_id="thread",
                command=command,
                cwd=directory,
                log_file=directory / "run.log",
                codex_bin="codex",
                run_id="run1",
                startup_file=directory / "run.startup.json" if startup else None,
            )

    def test_worker_logs_exit_and_marks_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            exit_code = self.run_with_successful_delivery(
                command=python_shell_command("print(12345)"), directory=directory
            )
            self.assertEqual(exit_code, 0)
            self.assertIn("12345", (directory / "run.log").read_text(encoding="utf-8"))
            event = wake_state.read_json(directory / "run.completion.json")
            self.assertEqual(event["delivery"]["state"], wake_state.DELIVERY_DELIVERED)
            self.assertGreaterEqual(event["duration_seconds"], 0)

    def test_worker_preserves_pipeline_failure(self) -> None:
        if os.name == "nt" or not wake_platform.shutil.which("bash"):
            self.skipTest("POSIX bash test")
        with tempfile.TemporaryDirectory() as tmp:
            exit_code = self.run_with_successful_delivery(command="false | cat", directory=Path(tmp))
            self.assertNotEqual(exit_code, 0)

    def test_worker_reports_running_only_after_process_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            exit_code = self.run_with_successful_delivery(
                command=python_shell_command("pass"), directory=directory, startup=True
            )
            status = wake_state.read_json(directory / "run.startup.json")
            self.assertEqual(exit_code, 0)
            self.assertEqual(status["state"], "running")
            self.assertIsInstance(status["process_pid"], int)

    def test_worker_startup_failure_is_explicit_and_not_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            startup = directory / "run.startup.json"
            with mock.patch.object(worker_runtime, "open_private_log", side_effect=PermissionError("denied")):
                with mock.patch.object(wake_run, "deliver_completion") as deliver:
                    exit_code = wake_run.run_worker(
                        thread_id="thread",
                        command="echo no",
                        cwd=directory,
                        log_file=directory / "run.log",
                        codex_bin="codex",
                        run_id="run1",
                        startup_file=startup,
                    )
            self.assertEqual(exit_code, wake_run.WORKER_STARTUP_FAILURE)
            self.assertEqual(wake_state.read_json(startup)["state"], "startup_failed")
            deliver.assert_not_called()

    def test_delivery_failure_remains_pending(self) -> None:
        def failed_queue(*args, before_attempt=None, after_failure=None, **kwargs):
            if before_attempt:
                before_attempt(1)
            if after_failure:
                after_failure(1, "queue down")
            raise RuntimeError("queue down")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with mock.patch.object(wake_run, "queue_wakeup", side_effect=failed_queue):
                exit_code = wake_run.run_worker(
                    thread_id="thread",
                    command=python_shell_command("pass"),
                    cwd=directory,
                    log_file=directory / "run.log",
                    codex_bin="codex",
                    run_id="run1",
                )
            event = wake_state.read_json(directory / "run.completion.json")
            self.assertEqual(exit_code, wake_run.WORKER_DELIVERY_FAILURE)
            self.assertEqual(event["delivery"]["state"], wake_state.DELIVERY_PENDING)
            self.assertEqual(event["delivery"]["last_error"], "queue down")

    def test_completion_write_failure_sends_minimal_wake_and_logs_error(self) -> None:
        messages: list[str] = []

        def capture_queue(_thread, message, _codex, **kwargs):
            messages.append(message)
            return 1

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with mock.patch.object(worker_runtime, "create_completion_event", side_effect=OSError("disk full")):
                with mock.patch.object(wake_run, "queue_wakeup", side_effect=capture_queue):
                    exit_code = wake_run.run_worker(
                        thread_id="thread",
                        command=python_shell_command("pass"),
                        cwd=directory,
                        log_file=directory / "run.log",
                        codex_bin="codex",
                        run_id="run1",
                    )
            self.assertEqual(exit_code, wake_run.WORKER_STATE_FAILURE)
            self.assertIn("[后台任务完成-系统提示]", messages[0])
            self.assertIn("exit_code: 0", messages[0])
            self.assertIn("completion persistence failed", (directory / "run.log").read_text())
            self.assertIn("disk full", (directory / "run.log").read_text())


class LauncherTests(unittest.TestCase):
    @mock.patch.object(wake_run, "_wait_for_startup")
    @mock.patch.object(wake_run.subprocess, "Popen")
    def test_launcher_returns_armed_only_after_running(
        self, popen: mock.Mock, wait: mock.Mock
    ) -> None:
        popen.return_value.pid = 42
        wait.return_value = {
            "state": "running",
            "run_id": "fixed-run-12",
            "worker_pid": 42,
            "process_pid": 84,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(wake_run, "preflight_codex_queue", return_value="/resolved/codex"):
                with mock.patch.object(wake_run.uuid, "uuid4") as uuid4:
                    uuid4.return_value.hex = "fixed-run-12-and-more"
                    result = wake_run.arm_watcher(
                        thread_id="thread",
                        command="echo ok",
                        cwd=Path(tmp),
                        log_dir=Path(tmp) / "state",
                        codex_bin="codex",
                    )
        self.assertEqual(result["status"], "armed")
        self.assertEqual(result["process_pid"], 84)
        self.assertIn("--startup-file", popen.call_args.args[0])

    def test_startup_failure_and_worker_exit_are_errors(self) -> None:
        worker = mock.Mock()
        worker.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "startup.json"
            wake_state.write_startup_status(
                status_file,
                state="startup_failed",
                run_id="run1",
                worker_pid=12,
                error="denied",
            )
            with self.assertRaisesRegex(RuntimeError, "denied"):
                wake_run._wait_for_startup(worker, status_file, timeout=1)
        worker.poll.return_value = 127
        with self.assertRaisesRegex(RuntimeError, "exit 127"):
            wake_run._wait_for_startup(worker, Path("/missing/startup.json"), timeout=1)

    def test_startup_timeout_is_error(self) -> None:
        worker = mock.Mock()
        worker.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "within 0s"):
                wake_run._wait_for_startup(worker, Path(tmp) / "missing.json", timeout=0)

    def test_invalid_startup_timeout_is_explicit(self) -> None:
        with mock.patch.dict(os.environ, {"WAKE_RUN_STARTUP_TIMEOUT": "nan"}):
            with self.assertRaisesRegex(RuntimeError, "finite number"):
                wake_run.startup_timeout_from_environment()

class ReplayTests(unittest.TestCase):
    def test_replay_delivers_pending_and_skips_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pending = wake_state.create_completion_event(
                directory / "pending.log",
                run_id="pending",
                thread_id="thread",
                command="echo ok",
                exit_code=0,
                launch_error=None,
                wake_id="wake-pending",
            )
            wake_state.update_delivery(
                pending, state=wake_state.DELIVERY_PENDING, attempts=2, last_error="previous failure"
            )
            delivered = wake_state.create_completion_event(
                directory / "delivered.log",
                run_id="delivered",
                thread_id="thread",
                command="echo ok",
                exit_code=0,
                launch_error=None,
                wake_id="wake-delivered",
            )
            wake_state.update_delivery(
                delivered, state=wake_state.DELIVERY_DELIVERED, attempts=1, last_error=None
            )
            with mock.patch.object(wake_run, "preflight_codex_queue", return_value="/codex"):
                with mock.patch.object(wake_run, "queue_wakeup", side_effect=successful_queue):
                    result = wake_run.replay_pending(log_dir=directory, codex_bin="codex")
            self.assertEqual(result["delivered"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(
                wake_state.read_json(pending)["delivery"]["state"], wake_state.DELIVERY_DELIVERED
            )
            self.assertEqual(wake_state.read_json(pending)["delivery"]["attempts"], 3)


class IntegrationTests(unittest.TestCase):
    def create_fake_codex(self, directory: Path) -> Path:
        capture = directory / "queue-calls.jsonl"
        fake_codex = directory / "codex"
        fake_codex.write_text(
            f"#!{sys.executable}\nimport json,sys\n"
            f"open({str(capture)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        return fake_codex

    def wait_for_delivery(self, completion_file: Path) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if completion_file.exists():
                event = wake_state.read_json(completion_file)
                if event["delivery"]["state"] == wake_state.DELIVERY_DELIVERED:
                    return event
            time.sleep(0.01)
        self.fail(f"completion was not delivered: {completion_file}")

    @unittest.skipIf(os.name == "nt", "POSIX executable integration test")
    def test_real_launcher_confirms_process_before_armed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake_codex = self.create_fake_codex(directory)
            state_dir = directory / "state"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                result = wake_run.arm_watcher(
                    thread_id="thread",
                    command=python_shell_command("import time; time.sleep(0.05); print('done')"),
                    cwd=directory,
                    log_dir=state_dir,
                    codex_bin=str(fake_codex),
                )
            self.assertEqual(result["status"], "armed")
            self.assertIsInstance(result["process_pid"], int)
            self.assertFalse(list(state_dir.glob("*.startup.json")))
            completion = Path(str(result["log_file"])).with_suffix(".completion.json")
            event = self.wait_for_delivery(completion)
            self.assertEqual(event["exit_code"], 0)

    @unittest.skipIf(os.name == "nt", "POSIX executable integration test")
    def test_real_launcher_rejects_worker_startup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake_codex = self.create_fake_codex(directory)
            with self.assertRaisesRegex(RuntimeError, "does not exist or is not a directory"):
                wake_run.arm_watcher(
                    thread_id="thread",
                    command="echo never-started",
                    cwd=directory / "missing-cwd",
                    log_dir=directory / "state",
                    codex_bin=str(fake_codex),
                )
            self.assertFalse((directory / "missing-cwd").exists())
            self.assertFalse((directory / "state").exists())

    @unittest.skipIf(os.name == "nt", "POSIX executable integration test")
    def test_worker_cli_sends_queue_and_persists_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            capture = directory / "queue.json"
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
                "--worker",
                "--command", python_shell_command("print(24680)"),
                "--thread-id", "thread",
                "--cwd", str(directory),
                "--log-file", str(directory / "run.log"),
                "--codex-bin", str(fake_codex),
                "--run-id", "run1",
            ], capture_output=True, text=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("wake_id：", json.loads(capture.read_text(encoding="utf-8"))[4])
            event = wake_state.read_json(directory / "run.completion.json")
            self.assertEqual(event["delivery"]["state"], wake_state.DELIVERY_DELIVERED)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell integration test")
    def test_windows_worker_executes_single_quoted_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            script = directory / "wake run test.ps1"
            script.write_text("Write-Output 'WINDOWS_WAKE_OK'\nexit 0\n", encoding="utf-8")
            with mock.patch.object(wake_run, "queue_wakeup", side_effect=successful_queue):
                exit_code = wake_run.run_worker(
                    thread_id="thread",
                    command=f"& '{script}'",
                    cwd=directory,
                    log_file=directory / "run.log",
                    codex_bin="codex",
                )
            self.assertEqual(exit_code, 0)


class LayoutTests(unittest.TestCase):
    def test_skill_layout_and_file_limits(self) -> None:
        self.assertTrue((ROOT / "SKILL.md").is_file())
        self.assertFalse((ROOT / ".codex-plugin").exists())
        for path in [*SCRIPTS.glob("*.py"), *Path(__file__).parent.glob("test_*.py")]:
            lines = len(path.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 500, f"{path} has {lines} lines")

    def test_runtime_waits_for_process_without_shell_true(self) -> None:
        source = (SCRIPTS / "wake_run_worker.py").read_text(encoding="utf-8")
        self.assertIn("process.wait()", source)
        self.assertNotIn("shell=True", source)


if __name__ == "__main__":
    unittest.main()
