from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from wake_run_app_server import AppServerClient, AppServerConfig
from wake_run_goal import CodexGoalRpc


FAKE_SERVER = r'''#!{python}
import json
import sys

goal = {{
    "threadId": "thread1", "objective": "finish", "status": "active",
    "tokenBudget": None, "tokensUsed": 5, "timeUsedSeconds": 2,
    "createdAt": 10, "updatedAt": 10,
}}
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialized":
        continue
    if method == "initialize":
        result = {{"userAgent": "fake", "codexHome": "/tmp", "platformFamily": "unix", "platformOs": "linux"}}
    elif method == "thread/goal/get":
        result = {{"goal": goal}}
    elif method == "thread/goal/set":
        goal = {{**goal, "status": message["params"]["status"], "updatedAt": goal["updatedAt"] + 1}}
        result = {{"goal": goal}}
    else:
        print(json.dumps({{"id": message.get("id"), "error": {{"message": "unknown"}}}}), flush=True)
        continue
    print(json.dumps({{"id": message["id"], "result": result}}), flush=True)
'''


class AppServerClientTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX executable protocol fixture")
    def test_persistent_jsonl_session_handles_goal_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex"
            executable.write_text(FAKE_SERVER.format(python=sys.executable), encoding="utf-8")
            executable.chmod(0o755)
            with CodexGoalRpc(str(executable)) as rpc:
                before = rpc.get_goal("thread1")
                paused = rpc.set_status("thread1", "paused")
                after = rpc.get_goal("thread1")
            self.assertEqual(before["status"], "active")
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(after["status"], "paused")

    def test_invalid_timeout_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"WAKE_RUN_GOAL_TIMEOUT": "nan"}):
            with self.assertRaisesRegex(RuntimeError, "finite number"):
                AppServerConfig.from_environment("codex")

    def test_unstarted_client_rejects_requests(self) -> None:
        client = AppServerClient(AppServerConfig("codex", 1))
        with self.assertRaisesRegex(RuntimeError, "not started"):
            client.request("thread/goal/get", {"threadId": "thread1"})


if __name__ == "__main__":
    unittest.main()
