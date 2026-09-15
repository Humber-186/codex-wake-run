from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wake_run_core as core
import wake_run_goal as goal
import wake_run_goal_state as goal_state_storage
import wake_run_state as state


def goal_state(status: str = "active") -> dict[str, object]:
    return {
        "threadId": "thread1",
        "objective": "finish reliable work",
        "status": status,
        "tokenBudget": None,
        "tokensUsed": 100,
        "timeUsedSeconds": 10,
        "createdAt": 1000,
        "updatedAt": 1000,
    }


class FakeGoalRpc:
    def __init__(self, current: dict[str, object] | None) -> None:
        self.current = current
        self.set_calls: list[str] = []
        self.ignore_pause = False
        self.fail_active = False

    def __enter__(self) -> "FakeGoalRpc":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get_goal(self, _thread_id: str) -> dict[str, object] | None:
        return dict(self.current) if self.current is not None else None

    def set_status(self, _thread_id: str, status: str) -> dict[str, object]:
        self.set_calls.append(status)
        if status == "active" and self.fail_active:
            raise RuntimeError("restore unavailable")
        if self.current is None:
            raise RuntimeError("goal missing")
        if status == "paused" and self.ignore_pause:
            return dict(self.current)
        self.current = {**self.current, "status": status, "updatedAt": int(self.current["updatedAt"]) + 1}
        return dict(self.current)


def context(directory: Path, rpc: FakeGoalRpc) -> goal.GoalGuardContext:
    return goal.GoalGuardContext(
        thread_id="thread1",
        codex_bin="codex",
        lease_root=directory,
        rpc_factory=lambda _codex: rpc,
    )


class GoalLeaseTests(unittest.TestCase):
    def test_active_goal_is_paused_verified_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeGoalRpc(goal_state())
            ctx = context(Path(tmp), rpc)
            guard = goal.acquire_goal_guard(ctx, run_id="run1", policy="auto")
            self.assertEqual(guard.mode, "paused")
            self.assertTrue(guard.verified)
            self.assertEqual(guard.as_dict()["runtime_scope"], "detached")
            self.assertEqual(
                guard.as_dict()["current_turn_accounting"], "not_guaranteed"
            )
            self.assertEqual(rpc.current["status"], "paused")
            outcome = goal.release_goal_guard(ctx, lease_id=str(guard.lease_id))
            self.assertEqual(outcome, goal.LEASE_PHASE_RESTORED)
            self.assertEqual(rpc.current["status"], "active")
            lease = state.read_json(goal.goal_lease_path(ctx))
            self.assertEqual(lease["phase"], goal.LEASE_PHASE_RESTORED)

    def test_non_active_goal_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeGoalRpc(goal_state("paused"))
            guard = goal.acquire_goal_guard(context(Path(tmp), rpc), run_id="run1", policy="auto")
            self.assertEqual(guard.mode, "not_needed")
            self.assertEqual(rpc.set_calls, [])

    def test_require_rejects_missing_or_non_active_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for current in (None, goal_state("blocked")):
                rpc = FakeGoalRpc(current)
                with self.assertRaisesRegex(RuntimeError, "needs an active Goal"):
                    goal.acquire_goal_guard(
                        context(Path(tmp), rpc), run_id="run1", policy="require"
                    )

    def test_parallel_watchers_share_lease_and_first_release_restores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeGoalRpc(goal_state())
            ctx = context(Path(tmp), rpc)
            first = goal.acquire_goal_guard(ctx, run_id="run1", policy="auto")
            second = goal.acquire_goal_guard(ctx, run_id="run2", policy="auto")
            self.assertEqual(first.lease_id, second.lease_id)
            lease = state.read_json(goal.goal_lease_path(ctx))
            self.assertEqual(
                [holder["run_id"] for holder in lease["holders"]], ["run1", "run2"]
            )
            goal.release_goal_guard(ctx, lease_id=str(second.lease_id))
            self.assertEqual(rpc.current["status"], "active")

    def test_user_goal_change_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeGoalRpc(goal_state())
            ctx = context(Path(tmp), rpc)
            guard = goal.acquire_goal_guard(ctx, run_id="run1", policy="auto")
            rpc.current = {**goal_state("paused"), "objective": "user replacement", "createdAt": 2000}
            outcome = goal.release_goal_guard(ctx, lease_id=str(guard.lease_id))
            self.assertEqual(outcome, goal.LEASE_PHASE_SKIPPED)
            self.assertEqual(rpc.current["objective"], "user replacement")
            self.assertEqual(rpc.current["status"], "paused")

    def test_pause_verification_failure_rolls_back_without_stale_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeGoalRpc(goal_state())
            rpc.ignore_pause = True
            ctx = context(Path(tmp), rpc)
            with self.assertRaisesRegex(RuntimeError, "pause verification failed"):
                goal.acquire_goal_guard(ctx, run_id="run1", policy="auto")
            self.assertEqual(rpc.current["status"], "active")
            self.assertFalse(goal.goal_lease_path(ctx).exists())

    def test_restore_failure_remains_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeGoalRpc(goal_state())
            ctx = context(Path(tmp), rpc)
            guard = goal.acquire_goal_guard(ctx, run_id="run1", policy="auto")
            rpc.fail_active = True
            with self.assertRaisesRegex(RuntimeError, "restore unavailable"):
                goal.release_goal_guard(ctx, lease_id=str(guard.lease_id))
            lease = state.read_json(goal.goal_lease_path(ctx))
            self.assertEqual(lease["phase"], goal.LEASE_PHASE_PAUSED)
            self.assertEqual(rpc.current["status"], "paused")

    def test_missing_paused_lease_is_not_reported_as_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeGoalRpc(goal_state())
            ctx = context(Path(tmp), rpc)
            guard = goal.acquire_goal_guard(ctx, run_id="run1", policy="auto")
            goal.goal_lease_path(ctx).unlink()
            with self.assertRaisesRegex(RuntimeError, "Goal lease is missing"):
                goal.release_goal_guard(ctx, lease_id=str(guard.lease_id))
            self.assertEqual(rpc.current["status"], "paused")

    def test_missing_lease_blocks_target_spawn_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeGoalRpc(goal_state())
            ctx = context(Path(tmp), rpc)
            guard = goal.acquire_goal_guard(ctx, run_id="run1", policy="auto")
            goal.goal_lease_path(ctx).unlink()
            with self.assertRaisesRegex(RuntimeError, "disappeared before target start"):
                goal.mark_goal_holder_spawning(
                    ctx,
                    run_id="run1",
                    lease_id=str(guard.lease_id),
                )

    def test_version_one_holder_is_upgraded_without_losing_started_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeGoalRpc(goal_state())
            ctx = context(Path(tmp), rpc)
            goal.acquire_goal_guard(ctx, run_id="run1", policy="auto")
            path = goal.goal_lease_path(ctx)
            lease = state.read_json(path)
            legacy_holder = {**lease["holders"][0], "target_started": True}
            legacy_holder.pop("phase")
            legacy_holder.pop("target_pid")
            state.atomic_write_json(path, {
                **lease,
                "schema_version": 1,
                "holders": [legacy_holder],
            })
            upgraded = goal_state_storage.read_lease(path, "thread1")
            self.assertEqual(upgraded["schema_version"], 2)
            self.assertEqual(upgraded["holders"][0]["phase"], "running")

    def test_replay_restores_abandoned_pre_completion_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            leases = directory / "leases"
            logs = directory / "logs"
            rpc = FakeGoalRpc(goal_state())
            ctx = context(leases, rpc)
            goal.acquire_goal_guard(
                ctx,
                run_id="run1",
                policy="auto",
                worker_pid=999_999_999,
                completion_file=logs / "run.completion.json",
            )
            with mock.patch.object(
                goal_state_storage, "process_is_alive", return_value=False
            ):
                result = goal.recover_abandoned_goal_leases(
                    log_dir=logs,
                    codex_bin="codex",
                    lease_root=leases,
                    rpc_factory=lambda _codex: rpc,
                )
            self.assertEqual(len(result["restored"]), 1)
            self.assertEqual(rpc.current["status"], "active")

    def test_replay_retains_live_or_persisted_holder(self) -> None:
        for completion_exists, process_alive in ((False, True), (True, False)):
            with self.subTest(
                completion_exists=completion_exists, process_alive=process_alive
            ):
                with tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    leases = directory / "leases"
                    logs = directory / "logs"
                    completion = logs / "run.completion.json"
                    rpc = FakeGoalRpc(goal_state())
                    goal.acquire_goal_guard(
                        context(leases, rpc),
                        run_id="run1",
                        policy="auto",
                        worker_pid=123,
                        completion_file=completion,
                    )
                    if completion_exists:
                        logs.mkdir()
                        completion.write_text("{}", encoding="utf-8")
                    with mock.patch.object(
                        goal_state_storage,
                        "process_is_alive",
                        return_value=process_alive,
                    ):
                        result = goal.recover_abandoned_goal_leases(
                            log_dir=logs,
                            codex_bin="codex",
                            lease_root=leases,
                            rpc_factory=lambda _codex: rpc,
                        )
                    self.assertEqual(len(result["retained"]), 1)
                    self.assertEqual(rpc.current["status"], "paused")

    def test_replay_keeps_goal_paused_for_orphaned_started_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            leases = directory / "leases"
            logs = directory / "logs"
            rpc = FakeGoalRpc(goal_state())
            ctx = context(leases, rpc)
            guard = goal.acquire_goal_guard(
                ctx,
                run_id="run1",
                policy="auto",
                worker_pid=999_999_999,
                completion_file=logs / "run.completion.json",
            )
            goal.mark_goal_holder_spawning(
                ctx,
                run_id="run1",
                lease_id=str(guard.lease_id),
            )
            with mock.patch.object(
                goal_state_storage, "process_is_alive", return_value=False
            ):
                result = goal.recover_abandoned_goal_leases(
                    log_dir=logs,
                    codex_bin="codex",
                    lease_root=leases,
                    rpc_factory=lambda _codex: rpc,
                )
            self.assertEqual(len(result["orphaned"]), 1)
            self.assertEqual(rpc.current["status"], "paused")
            lease = state.read_json(goal.goal_lease_path(ctx))
            self.assertEqual(lease["phase"], goal.LEASE_PHASE_ORPHANED)

    def test_orphan_holder_blocks_other_holder_release_and_join(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            logs = directory / "logs"
            rpc = FakeGoalRpc(goal_state())
            ctx = context(directory / "leases", rpc)
            first = goal.acquire_goal_guard(
                ctx,
                run_id="run1",
                policy="auto",
                worker_pid=101,
                completion_file=logs / "run1.completion.json",
            )
            goal.acquire_goal_guard(
                ctx,
                run_id="run2",
                policy="auto",
                worker_pid=202,
                completion_file=logs / "run2.completion.json",
            )
            goal.mark_goal_holder_spawning(
                ctx,
                run_id="run1",
                lease_id=str(first.lease_id),
            )
            logs.mkdir()
            (logs / "run2.completion.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(
                goal_state_storage, "process_is_alive", return_value=False
            ):
                with self.assertRaisesRegex(RuntimeError, "orphaned holder"):
                    goal.release_goal_guard(ctx, lease_id=str(first.lease_id))
                with self.assertRaisesRegex(RuntimeError, "orphaned"):
                    goal.acquire_goal_guard(
                        ctx,
                        run_id="run3",
                        policy="auto",
                        worker_pid=303,
                        completion_file=logs / "run3.completion.json",
                    )
            self.assertEqual(rpc.current["status"], "paused")
            lease = state.read_json(goal.goal_lease_path(ctx))
            self.assertEqual(lease["phase"], goal.LEASE_PHASE_ORPHANED)


class GoalDeliveryTests(unittest.TestCase):
    def create_event(self, directory: Path) -> Path:
        return state.create_completion_event(
            directory / "run.log",
            run_id="run1",
            thread_id="thread1",
            command="echo ok",
            exit_code=0,
            launch_error=None,
            wake_id="wake1",
            goal_guard={"mode": "paused", "verified": True, "lease_id": "lease1"},
        )

    def test_queue_completes_before_goal_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = self.create_event(Path(tmp))
            order: list[str] = []

            def queued(*_args, before_attempt=None, **_kwargs):
                order.append("queue")
                if before_attempt:
                    before_attempt(1)
                return 1

            def released(*_args, **_kwargs):
                order.append("release")
                return goal.LEASE_PHASE_RESTORED

            with mock.patch.object(core, "queue_wakeup", side_effect=queued):
                with mock.patch.object(core, "release_goal_guard", side_effect=released):
                    core.deliver_completion(event, "codex")
            self.assertEqual(order, ["queue", "release"])
            payload = state.read_json(event)
            self.assertEqual(payload["delivery"]["state"], state.DELIVERY_DELIVERED)
            self.assertEqual(payload["goal_release"]["state"], state.GOAL_RELEASE_RESTORED)

    def test_queue_failure_keeps_goal_release_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = self.create_event(Path(tmp))
            with mock.patch.object(core, "queue_wakeup", side_effect=RuntimeError("queue down")):
                with mock.patch.object(core, "release_goal_guard") as release:
                    with self.assertRaisesRegex(RuntimeError, "queue down"):
                        core.deliver_completion(event, "codex")
            release.assert_not_called()
            payload = state.read_json(event)
            self.assertEqual(payload["goal_release"]["state"], state.GOAL_RELEASE_PENDING)

    def test_release_retry_does_not_queue_duplicate_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = self.create_event(Path(tmp))
            state.update_delivery(event, state=state.DELIVERY_DELIVERED, attempts=1, last_error=None)
            with mock.patch.object(core, "queue_wakeup") as queued:
                with mock.patch.object(
                    core, "release_goal_guard", return_value=goal.LEASE_PHASE_RESTORED
                ):
                    core.deliver_completion(event, "codex")
            queued.assert_not_called()

    def test_release_error_stays_retryable_after_wake_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = self.create_event(Path(tmp))
            state.update_delivery(
                event,
                state=state.DELIVERY_DELIVERED,
                attempts=1,
                last_error=None,
            )
            with mock.patch.object(
                core,
                "release_goal_guard",
                side_effect=RuntimeError("Goal lease is missing"),
            ):
                with self.assertRaisesRegex(RuntimeError, "Goal lease is missing"):
                    core.deliver_completion(event, "codex")
            payload = state.read_json(event)
            self.assertEqual(payload["goal_release"]["state"], state.GOAL_RELEASE_RETRYING)
            self.assertIn("Goal lease is missing", payload["goal_release"]["last_error"])


if __name__ == "__main__":
    unittest.main()
