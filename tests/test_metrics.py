from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wake_run_metrics as metrics
import wake_run_core as wake_run


class ExecutionMetricsTests(unittest.TestCase):
    def test_collect_metrics_calculates_wall_and_child_cpu_deltas(self) -> None:
        with mock.patch.object(metrics.time, "monotonic", return_value=13.0):
            with mock.patch.object(metrics, "cpu_usage_snapshot", return_value=(5.5, 7.0)):
                result = metrics.collect_metrics(10.0, (1.5, 2.0))
        self.assertEqual(result, metrics.ExecutionMetrics(3.0, 4.0, 5.0))

    def test_collect_metrics_marks_cpu_unavailable_without_snapshot(self) -> None:
        with mock.patch.object(metrics.time, "monotonic", return_value=13.0):
            result = metrics.collect_metrics(10.0, None)
        self.assertEqual(result, metrics.ExecutionMetrics(3.0, None, None))

    def test_wake_message_omits_unavailable_cpu_metrics(self) -> None:
        message = wake_run.build_wake_message(
            "echo ok", 0, Path("/tmp/run.log"), duration_seconds=1.0,
        )
        self.assertIn("wall：1.000s", message)
        self.assertNotIn("user：", message)
        self.assertNotIn("sys：", message)

    def test_wake_message_preserves_complete_delivery_contract(self) -> None:
        message = wake_run.build_wake_message(
            "python train.py", 0, Path("/tmp/run.log"), duration_seconds=1.23456,
            user_seconds=0.321, system_seconds=0.045, run_id="run1", wake_id="wake1",
        )
        self.assertEqual(message, "\n".join([
            "[后台任务完成-系统提示]",
            "任务：python train.py",
            "日志：/tmp/run.log",
            "exit_code: 0",
            "wall：1.235s",
            "user：0.321s",
            "sys：0.045s",
            "run_id：run1",
            "wake_id：wake1",
        ]))


if __name__ == "__main__":
    unittest.main()
