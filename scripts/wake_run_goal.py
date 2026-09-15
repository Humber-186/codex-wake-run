"""Durable Goal pause leases for wake-run."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from wake_run_app_server import CodexGoalRpc
from wake_run_goal_state import (
    HOLDER_PHASE_PREPARED,
    HOLDER_PHASE_RUNNING,
    HOLDER_PHASE_SPAWNING,
    LEASE_PHASE_ORPHANED,
    LEASE_PHASE_PAUSED,
    LEASE_PHASE_RESTORED,
    LEASE_PHASE_SKIPPED,
    LEASE_SCHEMA_VERSION,
    TERMINAL_PHASES,
    holder as _holder,
    holder_is_abandoned as _holder_is_abandoned,
    holder_is_orphaned as _holder_is_orphaned,
    lease_holders as _lease_holders,
    persist_orphaned as _persist_orphaned,
    raise_if_orphaned as _raise_if_orphaned,
    read_lease as _read_existing_lease,
)
from wake_run_state import atomic_write_json, process_lock, read_json, utc_now

GOAL_POLICY_AUTO = "auto"
GOAL_POLICY_REQUIRE = "require"
GOAL_POLICY_IGNORE = "ignore"
GOAL_POLICIES = frozenset({GOAL_POLICY_AUTO, GOAL_POLICY_REQUIRE, GOAL_POLICY_IGNORE})


class GoalRpc(Protocol):
    def get_goal(self, thread_id: str) -> dict[str, object] | None: ...

    def set_status(self, thread_id: str, status: str) -> dict[str, object]: ...


GoalRpcFactory = Callable[[str], GoalRpc]


@dataclass(frozen=True)
class GoalGuardContext:
    thread_id: str
    codex_bin: str
    lease_root: Path | None = None
    rpc_factory: GoalRpcFactory = CodexGoalRpc


@dataclass(frozen=True)
class GoalGuard:
    mode: str
    verified: bool
    lease_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "mode": self.mode,
            "verified": self.verified,
            "lease_id": self.lease_id,
        }
        if self.mode == "paused":
            result.update({
                "runtime_scope": "detached",
                "current_turn_accounting": "not_guaranteed",
            })
        return result


def goal_lease_root() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return codex_home.expanduser().resolve() / "wake-run" / "goal-leases"
def goal_lease_path(context: GoalGuardContext) -> Path:
    digest = hashlib.sha256(context.thread_id.encode("utf-8")).hexdigest()
    return (context.lease_root or goal_lease_root()) / f"{digest}.json"


def acquire_goal_guard(
    context: GoalGuardContext,
    *,
    run_id: str,
    policy: str,
    worker_pid: int = 0,
    completion_file: Path | None = None,
) -> GoalGuard:
    _validate_policy(policy)
    if policy == GOAL_POLICY_IGNORE:
        return GoalGuard(mode="ignored", verified=False)
    lease_path = goal_lease_path(context)
    with process_lock(lease_path.with_suffix(".lock"), "Goal lease", blocking=True):
        with context.rpc_factory(context.codex_bin) as rpc:
            existing = _read_existing_lease(lease_path, context.thread_id)
            if existing and existing.get("phase") not in TERMINAL_PHASES:
                return _join_lease(
                    rpc,
                    lease_path,
                    existing,
                    thread_id=context.thread_id,
                    holder=_holder(run_id, worker_pid, completion_file),
                )
            goal = rpc.get_goal(context.thread_id)
            if goal is None:
                if policy == GOAL_POLICY_REQUIRE:
                    raise RuntimeError("--goal-policy require needs an active Goal")
                return GoalGuard(mode="not_needed", verified=True)
            _validate_goal(goal, context.thread_id)
            if goal["status"] != "active":
                if policy == GOAL_POLICY_REQUIRE:
                    raise RuntimeError(
                        f"--goal-policy require needs an active Goal; current status is {goal['status']}"
                    )
                return GoalGuard(mode="not_needed", verified=True)
            return _create_pause_lease(
                rpc,
                lease_path,
                context.thread_id,
                holder=_holder(run_id, worker_pid, completion_file),
                original=goal,
            )


def release_goal_guard(context: GoalGuardContext, *, lease_id: str) -> str:
    lease_path = goal_lease_path(context)
    with process_lock(lease_path.with_suffix(".lock"), "Goal lease", blocking=True):
        if not lease_path.exists():
            raise RuntimeError(f"Goal lease is missing: {lease_path}")
        lease = _read_existing_lease(lease_path, context.thread_id)
        assert lease is not None
        if lease.get("lease_id") != lease_id:
            return LEASE_PHASE_SKIPPED
        phase = lease.get("phase")
        if phase in TERMINAL_PHASES:
            return str(phase)
        _raise_if_orphaned(lease_path, lease)
        with context.rpc_factory(context.codex_bin) as rpc:
            return _release_owned_lease(
                rpc,
                lease_path,
                lease,
                thread_id=context.thread_id,
            )


def cancel_goal_guard(context: GoalGuardContext, *, run_id: str, lease_id: str | None = None) -> str:
    lease_path = goal_lease_path(context)
    with process_lock(lease_path.with_suffix(".lock"), "Goal lease", blocking=True):
        if not lease_path.exists():
            if lease_id is not None:
                raise RuntimeError(f"Goal lease is missing during cancellation: {lease_path}")
            return "not_needed"
        lease = _read_existing_lease(lease_path, context.thread_id)
        assert lease is not None
        holders = _lease_holders(lease)
        if (
            not any(holder["run_id"] == run_id for holder in holders)
            or lease.get("phase") in TERMINAL_PHASES
        ):
            return "not_needed"
        remaining = [holder for holder in holders if holder["run_id"] != run_id]
        if remaining:
            updated = {**lease, "holders": remaining, "updated_at": utc_now()}
            atomic_write_json(lease_path, updated)
            _raise_if_orphaned(lease_path, updated)
            return "holder_removed"
        with context.rpc_factory(context.codex_bin) as rpc:
            return _release_owned_lease(
                rpc,
                lease_path,
                lease,
                thread_id=context.thread_id,
            )


def mark_goal_holder_spawning(
    context: GoalGuardContext,
    *,
    run_id: str,
    lease_id: str,
) -> None:
    _update_holder_phase(
        context,
        run_id=run_id,
        lease_id=lease_id,
        expected=frozenset({HOLDER_PHASE_PREPARED, HOLDER_PHASE_RUNNING}),
        phase=HOLDER_PHASE_SPAWNING,
        target_pid=None,
    )


def mark_goal_holder_running(
    context: GoalGuardContext,
    *,
    run_id: str,
    lease_id: str,
    target_pid: int,
) -> None:
    if target_pid <= 0:
        raise RuntimeError("Goal lease target_pid must be positive")
    _update_holder_phase(
        context,
        run_id=run_id,
        lease_id=lease_id,
        expected=frozenset({HOLDER_PHASE_SPAWNING}),
        phase=HOLDER_PHASE_RUNNING,
        target_pid=target_pid,
    )


def _update_holder_phase(
    context: GoalGuardContext,
    *,
    run_id: str,
    lease_id: str,
    expected: frozenset[str],
    phase: str,
    target_pid: int | None,
) -> None:
    lease_path = goal_lease_path(context)
    with process_lock(lease_path.with_suffix(".lock"), "Goal lease", blocking=True):
        if not lease_path.exists():
            raise RuntimeError(f"Goal lease disappeared before target start: {lease_path}")
        lease = _read_existing_lease(lease_path, context.thread_id)
        assert lease is not None
        if lease.get("lease_id") != lease_id:
            raise RuntimeError("Goal lease changed before target start")
        if lease.get("phase") != LEASE_PHASE_PAUSED:
            raise RuntimeError(f"Goal lease is not usable in phase {lease.get('phase')}")
        holders = _lease_holders(lease)
        matched = False
        updated: list[dict[str, object]] = []
        for holder in holders:
            if holder["run_id"] == run_id:
                if holder["phase"] not in expected:
                    raise RuntimeError(
                        f"Goal holder {run_id} cannot move from {holder['phase']} to {phase}"
                    )
                holder = {**holder, "phase": phase, "target_pid": target_pid}
                matched = True
            updated.append(holder)
        if not matched:
            raise RuntimeError(f"Goal lease has no holder for run {run_id}")
        atomic_write_json(lease_path, {**lease, "holders": updated, "updated_at": utc_now()})


def _create_pause_lease(
    rpc: GoalRpc,
    lease_path: Path,
    thread_id: str,
    *,
    holder: dict[str, object],
    original: dict[str, object],
) -> GoalGuard:
    lease_id = uuid.uuid4().hex
    lease = {
        "schema_version": LEASE_SCHEMA_VERSION,
        "lease_id": lease_id,
        "thread_id": thread_id,
        "original": original,
        "paused_snapshot": None,
        "holders": [holder],
        "phase": "prepared",
        "updated_at": utc_now(),
    }
    atomic_write_json(lease_path, lease)
    try:
        rpc.set_status(thread_id, "paused")
        paused = rpc.get_goal(thread_id)
        _verify_paused(original, paused, thread_id)
    except Exception as error:
        _rollback_pause(
            rpc,
            lease_path,
            lease,
            thread_id=thread_id,
            cause=error,
        )
        raise
    atomic_write_json(lease_path, {
        **lease,
        "paused_snapshot": paused,
        "phase": LEASE_PHASE_PAUSED,
        "updated_at": utc_now(),
    })
    return GoalGuard(mode="paused", verified=True, lease_id=lease_id)


def _join_lease(
    rpc: GoalRpc,
    lease_path: Path,
    lease: dict[str, object],
    *,
    thread_id: str,
    holder: dict[str, object],
) -> GoalGuard:
    _raise_if_orphaned(lease_path, lease)
    if lease.get("phase") != LEASE_PHASE_PAUSED:
        raise RuntimeError(f"Goal lease is not joinable in phase {lease.get('phase')}")
    paused = rpc.get_goal(thread_id)
    snapshot = lease.get("paused_snapshot")
    if not isinstance(snapshot, dict) or not _same_paused_snapshot(snapshot, paused):
        raise RuntimeError("Existing Goal lease conflicts with the current Goal")
    holders = _lease_holders(lease)
    run_id = holder["run_id"]
    if not any(item["run_id"] == run_id for item in holders):
        holders.append(holder)
        atomic_write_json(lease_path, {**lease, "holders": holders, "updated_at": utc_now()})
    return GoalGuard(mode="paused", verified=True, lease_id=str(lease["lease_id"]))


def _release_owned_lease(
    rpc: GoalRpc,
    lease_path: Path,
    lease: dict[str, object],
    *,
    thread_id: str,
) -> str:
    current = rpc.get_goal(thread_id)
    snapshot = lease.get("paused_snapshot")
    original = lease.get("original")
    if not isinstance(original, dict):
        raise RuntimeError("Goal lease original snapshot is invalid")
    if _same_identity(original, current) and current.get("status") == "active":
        return _finish_lease(lease_path, lease, LEASE_PHASE_RESTORED)
    paused_matches = isinstance(snapshot, dict) and _same_paused_snapshot(snapshot, current)
    prepared_matches = (
        lease.get("phase") in {"prepared", "rollback_failed"}
        and _same_identity(original, current)
        and current.get("status") == "paused"
    )
    if not paused_matches and not prepared_matches:
        return _finish_lease(lease_path, lease, LEASE_PHASE_SKIPPED)
    rpc.set_status(thread_id, "active")
    restored = rpc.get_goal(thread_id)
    if not _same_identity(original, restored) or restored.get("status") != "active":
        raise RuntimeError("Goal restore verification failed")
    return _finish_lease(lease_path, lease, LEASE_PHASE_RESTORED)


def _rollback_pause(
    rpc: GoalRpc,
    lease_path: Path,
    lease: dict[str, object],
    *,
    thread_id: str,
    cause: Exception,
) -> None:
    current = rpc.get_goal(thread_id)
    original = lease["original"]
    if isinstance(original, dict) and _same_identity(original, current):
        if current.get("status") == "active":
            lease_path.unlink(missing_ok=True)
            return
        if current.get("status") == "paused":
            rpc.set_status(thread_id, "active")
            restored = rpc.get_goal(thread_id)
            if _same_identity(original, restored) and restored.get("status") == "active":
                lease_path.unlink(missing_ok=True)
                return
    atomic_write_json(lease_path, {
        **lease,
        "phase": "rollback_failed",
        "last_error": f"{type(cause).__name__}: {cause}",
        "updated_at": utc_now(),
    })
    raise RuntimeError("Goal pause failed and rollback could not be verified") from cause


def _finish_lease(path: Path, lease: dict[str, object], phase: str) -> str:
    atomic_write_json(path, {**lease, "phase": phase, "updated_at": utc_now()})
    return phase


def recover_abandoned_goal_leases(
    *,
    log_dir: Path,
    codex_bin: str,
    lease_root: Path | None = None,
    rpc_factory: GoalRpcFactory = CodexGoalRpc,
) -> dict[str, object]:
    root = lease_root or goal_lease_root()
    restored: list[str] = []
    retained: list[str] = []
    orphaned: list[str] = []
    failures: dict[str, str] = {}
    if not root.exists():
        return {
            "restored": restored, "retained": retained,
            "orphaned": orphaned, "failures": failures,
        }
    for path in sorted(root.glob("*.json")):
        try:
            outcome = _recover_lease_file(
                path,
                log_dir.resolve(),
                codex_bin,
                rpc_factory=rpc_factory,
            )
            if outcome == LEASE_PHASE_RESTORED:
                restored.append(path.name)
            elif outcome == "retained":
                retained.append(path.name)
            elif outcome == "orphaned":
                orphaned.append(path.name)
        except Exception as error:
            failures[path.name] = f"{type(error).__name__}: {error}"
    return {
        "restored": restored, "retained": retained,
        "orphaned": orphaned, "failures": failures,
    }


def _recover_lease_file(
    path: Path,
    log_dir: Path,
    codex_bin: str,
    *,
    rpc_factory: GoalRpcFactory,
) -> str:
    with process_lock(path.with_suffix(".lock"), "Goal lease", blocking=True):
        raw = read_json(path)
        thread_id_value = raw.get("thread_id")
        if not isinstance(thread_id_value, str):
            raise RuntimeError(f"Goal lease has no valid thread_id: {path}")
        lease = _read_existing_lease(path, thread_id_value)
        assert lease is not None
        if lease.get("phase") in TERMINAL_PHASES:
            return "terminal"
        holders = _lease_holders(lease)
        if not any(_holder_is_relevant(holder, log_dir) for holder in holders):
            return "unrelated"
        if lease.get("phase") == LEASE_PHASE_ORPHANED:
            return "orphaned"
        if any(_holder_is_orphaned(holder, log_dir) for holder in holders):
            _persist_orphaned(path, lease)
            return "orphaned"
        abandoned = [holder for holder in holders if _holder_is_abandoned(holder, log_dir)]
        if not abandoned:
            return "retained"
        remaining = [holder for holder in holders if holder not in abandoned]
        if remaining:
            atomic_write_json(path, {**lease, "holders": remaining, "updated_at": utc_now()})
            return "retained"
        thread_id = thread_id_value
        with rpc_factory(codex_bin) as rpc:
            return _release_owned_lease(rpc, path, lease, thread_id=thread_id)


def _holder_is_relevant(holder: dict[str, object], log_dir: Path) -> bool:
    completion = holder.get("completion_file")
    return isinstance(completion, str) and Path(completion).parent == log_dir


def _validate_goal(goal: dict[str, object], thread_id: str) -> None:
    required = ("threadId", "objective", "status", "createdAt", "updatedAt", "tokensUsed", "timeUsedSeconds")
    missing = [key for key in required if key not in goal]
    if missing:
        raise RuntimeError(f"Goal is missing fields: {', '.join(missing)}")
    if goal["threadId"] != thread_id:
        raise RuntimeError("Goal belongs to a different thread")


def _verify_paused(
    original: dict[str, object],
    paused: dict[str, object] | None,
    thread_id: str,
) -> None:
    if paused is None:
        raise RuntimeError("Goal disappeared while pausing")
    _validate_goal(paused, thread_id)
    if paused.get("status") != "paused" or not _same_identity(original, paused):
        raise RuntimeError("Goal pause verification failed")


def _same_identity(expected: dict[str, object], actual: dict[str, object] | None) -> bool:
    if actual is None:
        return False
    keys = ("threadId", "objective", "createdAt", "tokenBudget")
    return all(expected.get(key) == actual.get(key) for key in keys)


def _same_paused_snapshot(expected: dict[str, object], actual: dict[str, object] | None) -> bool:
    if actual is None or actual.get("status") != "paused":
        return False
    keys = (
        "threadId", "objective", "createdAt", "tokenBudget", "updatedAt",
        "tokensUsed", "timeUsedSeconds",
    )
    return all(expected.get(key) == actual.get(key) for key in keys)


def _validate_policy(policy: str) -> None:
    if policy not in GOAL_POLICIES:
        raise RuntimeError(f"Invalid Goal policy: {policy}")
