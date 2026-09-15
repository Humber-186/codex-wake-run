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
import wake_run_monitor as monitor
import wake_run_state as wake_state
import wake_run_worker as worker


def policy_payload(*, allow_retry: bool = True) -> dict[str, object]:
    return {
        "schema_version": 1,
        "model": "gpt-5.6-luna",
        "instructions": "Retry only a clearly transient external failure.",
        "allowed_actions": ["retry_exact"] if allow_retry else [],
        "max_exact_retries": 1 if allow_retry else 0,
        "log_tail_bytes": 4096,
    }


def write_policy(directory: Path, *, allow_retry: bool = True) -> Path:
    path = directory / "monitor-policy.json"
    path.write_text(json.dumps(policy_payload(allow_retry=allow_retry)), encoding="utf-8")
    return path


def completed_codex(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["codex"], 0, stdout, "")


def python_shell_command(source: str) -> str:
    if os.name == "nt":
        executable = str(Path(sys.executable)).replace("'", "''")
        return f"& '{executable}' -c '{source.replace("'", "''")}'"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


class MonitorPolicyTests(unittest.TestCase):
    def test_loads_explicit_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = monitor.load_monitor_policy(write_policy(Path(tmp)))
        self.assertEqual(policy.model, "gpt-5.6-luna")
        self.assertEqual(policy.allowed_actions, ("retry_exact",))
        self.assertEqual(policy.max_exact_retries, 1)

    def test_rejects_unknown_fields_and_inconsistent_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            payload = policy_payload()
            payload["silent_fallback"] = True
            path = directory / "unknown.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unknown=.*silent_fallback"):
                monitor.load_monitor_policy(path)

            payload = policy_payload(allow_retry=False)
            payload["max_exact_retries"] = 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "must be zero"):
                monitor.load_monitor_policy(path)

    def test_monitor_role_cannot_launch_or_replay(self) -> None:
        environment = {"WAKE_RUN_ROLE": "monitor", "WAKE_RUN_MONITOR_DEPTH": "1"}
        with self.assertRaisesRegex(RuntimeError, "may not launch"):
            monitor.reject_recursive_launch(environment)

    def test_invalid_monitor_depth_is_explicit(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Invalid WAKE_RUN_MONITOR_DEPTH"):
            monitor.reject_recursive_launch({"WAKE_RUN_MONITOR_DEPTH": "-1"})

    def test_invalid_monitor_timeout_is_explicit(self) -> None:
        with mock.patch.dict(os.environ, {"WAKE_RUN_MONITOR_TIMEOUT": "nan"}):
            with self.assertRaisesRegex(RuntimeError, "finite number"):
                monitor.monitor_timeout_from_environment()

    def test_cli_rejects_recursive_launch_before_preflight(self) -> None:
        environment = {
            **os.environ,
            "CODEX_THREAD_ID": "monitor-thread",
            "WAKE_RUN_ROLE": "monitor",
            "WAKE_RUN_MONITOR_DEPTH": "1",
        }
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--command", "echo never"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=environment,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("may not launch another wake-run", result.stderr)

    def test_cli_rejects_recursive_hidden_worker_mode(self) -> None:
        environment = {
            **os.environ,
            "WAKE_RUN_ROLE": "monitor",
            "WAKE_RUN_MONITOR_DEPTH": "1",
        }
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), "--worker", "--command", "echo never",
                "--thread-id", "root", "--log-file", "never.log",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=environment,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("may not launch another wake-run", result.stderr)


class MonitorSessionTests(unittest.TestCase):
    def create_runtime_plan(
        self,
        directory: Path,
        *,
        allow_retry: bool = True,
    ) -> monitor.RuntimeMonitorPlan:
        policy = monitor.load_monitor_policy(write_policy(directory, allow_retry=allow_retry))
        output = json.dumps({"type": "thread.started", "thread_id": "monitor-session"}) + "\n"
        def initialize(invocation: list[str], **_kwargs):
            response_index = invocation.index("--output-last-message") + 1
            Path(invocation[response_index]).write_text(
                json.dumps({"status": "monitor_ready"}), encoding="utf-8"
            )
            return completed_codex(output)

        with mock.patch.object(monitor, "_run_codex", side_effect=initialize):
            return monitor.create_monitor_session(
                policy=policy,
                run_id="run1",
                root_thread_id="root-thread",
                cwd=directory,
                log_dir=directory,
                resolved_codex="/codex",
            )

    @mock.patch.object(monitor.subprocess, "run")
    def test_codex_monitor_text_io_is_utf8(self, run: mock.Mock) -> None:
        run.return_value = completed_codex()
        monitor._run_codex(["codex", "exec", "-"], prompt="中文日志", run_id="run1")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_creates_hashed_runtime_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.create_runtime_plan(Path(tmp))
            loaded = monitor.read_runtime_plan(plan.path)
        self.assertEqual(loaded.session_id, "monitor-session")
        self.assertEqual(loaded.root_thread_id, "root-thread")
        self.assertEqual(loaded.policy_hash, plan.policy_hash)

    def test_detects_runtime_plan_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.create_runtime_plan(Path(tmp))
            payload = wake_state.read_json(plan.path)
            payload["depth"] = 2
            wake_state.atomic_write_json(plan.path, payload)
            with self.assertRaisesRegex(RuntimeError, "integrity check failed"):
                monitor.read_runtime_plan(plan.path)

    def write_monitor_response(self, invocation: list[str], payload: dict[str, str]) -> None:
        output_index = invocation.index("--output-last-message") + 1
        Path(invocation[output_index]).write_text(json.dumps(payload), encoding="utf-8")

    def test_successful_execution_requires_report_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plan = self.create_runtime_plan(directory)
            log = directory / "run.log"
            log.write_text("done", encoding="utf-8")
            request = worker.WorkerRequest(
                "root-thread", "echo ok", directory, log, "/codex", "run1", None, plan.path
            )
            response = {
                "action": "report_success",
                "summary": "completed",
                "reason": "exit code is zero",
                "failure_category": "none",
            }

            def run_codex(invocation: list[str], **_kwargs):
                self.write_monitor_response(invocation, response)
                return completed_codex()

            with mock.patch.object(monitor, "_run_codex", side_effect=run_codex) as codex_call:
                decision = monitor.triage_execution(request, worker.ExecutionResult(0, None, True), 0)
            invocation = codex_call.call_args.args[0]
        self.assertEqual(decision.action, "report_success")
        resume_index = invocation.index("resume")
        self.assertLess(invocation.index("--sandbox"), resume_index)
        self.assertEqual(invocation[invocation.index("--sandbox") + 1], "read-only")
        self.assertEqual(invocation[invocation.index("--cd") + 1], str(directory))
        self.assertLess(invocation.index("--skip-git-repo-check"), resume_index)

    def test_unauthorized_retry_is_protocol_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plan = self.create_runtime_plan(directory, allow_retry=False)
            log = directory / "run.log"
            log.write_text("failed", encoding="utf-8")
            request = worker.WorkerRequest(
                "root-thread", "false", directory, log, "/codex", "run1", None, plan.path
            )
            response = {
                "action": "retry_exact",
                "summary": "retry",
                "reason": "network issue",
                "failure_category": "transient_external",
            }

            def run_codex(invocation: list[str], **_kwargs):
                self.write_monitor_response(invocation, response)
                return completed_codex()

            with mock.patch.object(monitor, "_run_codex", side_effect=run_codex):
                with self.assertRaisesRegex(monitor.MonitorProtocolError, "without authorization"):
                    monitor.triage_execution(request, worker.ExecutionResult(7, None, True), 0)

    def test_wait_error_cannot_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plan = self.create_runtime_plan(directory)
            log = directory / "run.log"
            log.write_text("wait failed", encoding="utf-8")
            request = worker.WorkerRequest(
                "root-thread", "job", directory, log, "/codex", "run1", None, plan.path
            )
            response = {
                "action": "retry_exact",
                "summary": "retry",
                "reason": "wait failed",
                "failure_category": "transient_external",
            }

            def run_codex(invocation: list[str], **_kwargs):
                self.write_monitor_response(invocation, response)
                return completed_codex()

            with mock.patch.object(monitor, "_run_codex", side_effect=run_codex):
                with self.assertRaisesRegex(monitor.MonitorProtocolError, "completed process"):
                    monitor.triage_execution(
                        request,
                        worker.ExecutionResult(None, "OSError: wait failed", True),
                        0,
                    )


class MonitorWorkerTests(unittest.TestCase):
    def decision(self, action: str) -> worker.TriageDecision:
        return worker.TriageDecision(
            action, action, "test decision", "none", "gpt-5.6-luna", "session", "hash"
        )

    def test_authorized_exact_retry_reuses_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            marker = directory / "attempted"
            source = (
                "from pathlib import Path; "
                f"p=Path({str(marker)!r}); existed=p.exists(); p.write_text('x'); "
                "raise SystemExit(0 if existed else 9)"
            )
            request = worker.WorkerRequest(
                "thread", python_shell_command(source), directory, directory / "run.log",
                "codex", "run1", None, directory / "run.monitor.json",
            )
            decisions = iter([self.decision("retry_exact"), self.decision("report_success")])
            delivered: list[Path] = []
            result = worker.run_worker(
                request,
                deliver=lambda path, _codex: delivered.append(path),
                notify_state_failure=lambda _failure: 74,
                triage=lambda *_args: next(decisions),
            )
            event = wake_state.read_json(delivered[0])
        self.assertEqual(result, 0)
        self.assertEqual(len(event["execution_attempts"]), 2)
        self.assertEqual([item["action"] for item in event["monitor"]["triage"]], [
            "retry_exact", "report_success",
        ])

    def test_monitor_failure_is_persisted_and_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            request = worker.WorkerRequest(
                "thread", python_shell_command("print('ok')"), directory, directory / "run.log",
                "codex", "run1", None, directory / "run.monitor.json",
            )
            delivered: list[Path] = []
            result = worker.run_worker(
                request,
                deliver=lambda path, _codex: delivered.append(path),
                notify_state_failure=lambda _failure: 74,
                triage=lambda *_args: (_ for _ in ()).throw(RuntimeError("monitor unavailable")),
            )
            event = wake_state.read_json(delivered[0])
        self.assertEqual(result, worker.WORKER_MONITOR_FAILURE)
        self.assertIn("monitor unavailable", event["monitor"]["error"])


class MonitorIntegrationTests(unittest.TestCase):
    def create_fake_codex(self, directory: Path) -> Path:
        fake = directory / "codex"
        capture = directory / "queue.json"
        source = f"""#!{sys.executable}
import json, pathlib, sys
args = sys.argv[1:]
if args[:2] == ['queue', '--help']:
    raise SystemExit(0)
if args and args[0] == 'exec' and 'resume' not in args:
    output = pathlib.Path(args[args.index('--output-last-message') + 1])
    output.write_text(json.dumps({{'status': 'monitor_ready'}}))
    print(json.dumps({{'type': 'thread.started', 'thread_id': 'monitor-integration'}}))
    raise SystemExit(0)
if args and args[0] == 'exec' and 'resume' in args:
    output = pathlib.Path(args[args.index('--output-last-message') + 1])
    output.write_text(json.dumps({{
        'action': 'report_success', 'summary': 'integration complete',
        'reason': 'zero exit', 'failure_category': 'none'
    }}))
    raise SystemExit(0)
if args and args[0] == 'queue':
    pathlib.Path({str(capture)!r}).write_text(json.dumps(args))
    raise SystemExit(0)
raise SystemExit(2)
"""
        fake.write_text(source, encoding="utf-8")
        fake.chmod(0o755)
        return fake

    def wait_for_completion(self, path: Path) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                event = wake_state.read_json(path)
                if event["delivery"]["state"] == wake_state.DELIVERY_DELIVERED:
                    return event
            time.sleep(0.01)
        self.fail(f"monitor completion was not delivered: {path}")

    @unittest.skipIf(os.name == "nt", "POSIX executable integration test")
    def test_launcher_persists_monitor_summary_and_delivers_minimal_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = self.create_fake_codex(directory)
            policy = monitor.load_monitor_policy(write_policy(directory))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                armed = wake_run.arm_watcher(
                    thread_id="root-thread",
                    command=python_shell_command("print('done')"),
                    cwd=directory,
                    log_dir=directory / "state",
                    codex_bin=str(fake),
                    monitor_policy=policy,
                )
            completion = Path(str(armed["log_file"])).with_suffix(".completion.json")
            event = self.wait_for_completion(completion)
            queued = json.loads((directory / "queue.json").read_text(encoding="utf-8"))
        self.assertEqual(armed["monitor"]["session_id"], "monitor-integration")
        self.assertEqual(event["monitor"]["triage"][-1]["summary"], "integration complete")
        self.assertEqual(queued[queued.index("--thread") + 1], "root-thread")
        wake_message = queued[queued.index("--message") + 1]
        self.assertTrue(wake_message.startswith("[后台任务完成-系统提示]"))
        self.assertIn("exit_code: 0", wake_message)

    @mock.patch.object(wake_run.subprocess, "Popen")
    @mock.patch.object(wake_run, "create_monitor_session", side_effect=RuntimeError("monitor denied"))
    def test_monitor_creation_failure_prevents_target_start(
        self,
        _create: mock.Mock,
        popen: mock.Mock,
    ) -> None:
        policy = monitor.MonitorPolicy("gpt-5.6-luna", "escalate", (), 0, 1024)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with mock.patch.object(wake_run, "preflight_codex_queue", return_value="/codex"):
                with self.assertRaisesRegex(RuntimeError, "monitor denied"):
                    wake_run.arm_watcher(
                        thread_id="root",
                        command="echo never",
                        cwd=directory,
                        log_dir=directory / "state",
                        codex_bin="codex",
                        monitor_policy=policy,
                    )
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
