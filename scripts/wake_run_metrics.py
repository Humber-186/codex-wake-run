"""Execution timing metrics for wake-run commands."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

if os.name != "nt":
    import resource
else:
    resource = None


@dataclass(frozen=True)
class ExecutionMetrics:
    wall_seconds: float
    user_seconds: float | None
    system_seconds: float | None


def cpu_usage_snapshot() -> tuple[float, float] | None:
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime, usage.ru_stime


def collect_metrics(
    started_at: float,
    before_cpu: tuple[float, float] | None,
) -> ExecutionMetrics:
    wall_seconds = time.monotonic() - started_at
    if before_cpu is None:
        return ExecutionMetrics(wall_seconds, None, None)
    after_cpu = cpu_usage_snapshot()
    if after_cpu is None:
        return ExecutionMetrics(wall_seconds, None, None)
    return ExecutionMetrics(
        wall_seconds,
        after_cpu[0] - before_cpu[0],
        after_cpu[1] - before_cpu[1],
    )
