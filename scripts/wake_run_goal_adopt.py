"""Transfer an orphaned Goal holder to a verified recovery observer."""

from __future__ import annotations

from wake_run_goal import GoalGuardContext, goal_lease_path
from wake_run_goal_state import (
    HOLDER_PHASE_RUNNING,
    LEASE_PHASE_ORPHANED,
    LEASE_PHASE_PAUSED,
    holder_is_orphaned,
    lease_holders,
    read_lease,
)
from wake_run_state import atomic_write_json, process_lock, utc_now


def adopt_goal_holder(
    context: GoalGuardContext,
    *,
    run_id: str,
    lease_id: str,
    worker_pid: int,
    target_pid: int,
) -> None:
    path = goal_lease_path(context)
    with process_lock(path.with_suffix(".lock"), "Goal lease", blocking=True):
        lease = read_lease(path, context.thread_id)
        if lease is None or lease.get("lease_id") != lease_id:
            raise RuntimeError("Goal lease is missing or changed during adopt")
        if lease.get("phase") not in {LEASE_PHASE_PAUSED, LEASE_PHASE_ORPHANED}:
            raise RuntimeError(f"Goal lease cannot be adopted in phase {lease.get('phase')}")
        holders = lease_holders(lease)
        updated: list[dict[str, object]] = []
        matched = False
        for holder in holders:
            if holder["run_id"] != run_id:
                if holder_is_orphaned(holder):
                    raise RuntimeError("Another Goal holder is orphaned; lease must remain paused")
                updated.append(holder)
                continue
            if holder.get("target_pid") != target_pid or holder.get("phase") != HOLDER_PHASE_RUNNING:
                raise RuntimeError("Goal holder target identity changed before adopt")
            updated.append({**holder, "worker_pid": worker_pid})
            matched = True
        if not matched:
            raise RuntimeError(f"Goal lease has no running holder for {run_id}")
        atomic_write_json(path, {
            **lease,
            "phase": LEASE_PHASE_PAUSED,
            "holders": updated,
            "updated_at": utc_now(),
        })
