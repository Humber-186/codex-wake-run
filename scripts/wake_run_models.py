"""Immutable runtime records shared by launcher, worker, and monitor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkerRequest:
    thread_id: str
    command: str
    cwd: Path
    log_file: Path
    codex_bin: str
    run_id: str
    startup_file: Path | None
    monitor_plan_file: Path | None = None
    gate_file: Path | None = None
    launcher_pid: int | None = None
    runtime_file: Path | None = None
    stage_plan_file: Path | None = None


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int | None
    error: str | None
    startup_confirmed: bool
    duration_seconds: float | None = None
    user_seconds: float | None = None
    system_seconds: float | None = None
    terminal_state: str = "completed"
    stage_delivery: dict[str, object] | None = None


@dataclass(frozen=True)
class TriageDecision:
    action: str
    summary: str
    reason: str
    failure_category: str
    model: str
    session_id: str
    policy_hash: str


@dataclass(frozen=True)
class WorkerOutcome:
    result: ExecutionResult
    attempts: tuple[dict[str, object], ...]
    triage: tuple[dict[str, object], ...]
    monitor_status: str
    monitor_error: str | None
    duration_seconds: float
    user_seconds: float | None
    system_seconds: float | None


@dataclass(frozen=True)
class StateFailure:
    request: WorkerRequest
    outcome: WorkerOutcome
    wake_id: str
    error: str
    goal_guard: dict[str, object]
