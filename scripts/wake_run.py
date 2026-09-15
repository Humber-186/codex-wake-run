#!/usr/bin/env python3
"""CLI entrypoint for wake-run."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wake_run_core import deliver_completion, deliver_stage_event, replay_pending, run_worker  # noqa: E402
from wake_run_adopt import adopt_run  # noqa: E402
from wake_run_adopt_worker import run_adopted_worker  # noqa: E402
from wake_run_control import issue_control  # noqa: E402
from wake_run_goal import GOAL_POLICIES, GOAL_POLICY_AUTO  # noqa: E402
from wake_run_launcher import arm_watcher  # noqa: E402
from wake_run_monitor import load_monitor_policy, reject_recursive_launch  # noqa: E402
from wake_run_registry import list_runs, run_index_root, show_run  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a detached command and wake its Codex thread on stages or exit."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--command", help="Exact shell command to execute.")
    action.add_argument("--replay-pending", action="store_true", help="Retry undelivered wake events.")
    action.add_argument("--list", action="store_true", help="List runs started by the current thread.")
    action.add_argument("--show", metavar="RUN_ID", help="Show one persisted run.")
    action.add_argument("--stop", metavar="RUN_ID", help="Stop an active run and wake on cancellation.")
    action.add_argument("--detach", metavar="RUN_ID", help="Stop supervising while leaving the target running.")
    action.add_argument("--adopt", metavar="RUN_ID", help="Recover observation after the original worker is lost.")
    action.add_argument("--adopt-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for the command.")
    parser.add_argument("--log-dir", help="Run state directory; defaults to <cwd>/.codex-wake-run.")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable.")
    parser.add_argument(
        "--goal-policy",
        choices=sorted(GOAL_POLICIES),
        default=GOAL_POLICY_AUTO,
        help="Goal protection policy (default: auto).",
    )
    parser.add_argument(
        "--monitor-plan",
        help="Enable economical monitoring with an explicit JSON authorization plan.",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--thread-id", help=argparse.SUPPRESS)
    parser.add_argument("--log-file", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--startup-file", help=argparse.SUPPRESS)
    parser.add_argument("--gate-file", help=argparse.SUPPRESS)
    parser.add_argument("--launcher-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--monitor-plan-file", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-file", help=argparse.SUPPRESS)
    parser.add_argument("--stage-plan-file", help=argparse.SUPPRESS)
    parser.add_argument("--spec-file", help=argparse.SUPPRESS)
    parser.add_argument("--active", action="store_true", help="With --list, include only active runs.")
    parser.add_argument("--name", help="Optional human-readable name for a new run.")
    parser.add_argument("--stage-plan", help="JSON plan with ordered log_line_regex stages.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    reject_recursive_launch()
    if args.active and not args.list:
        raise SystemExit("--active requires --list")
    if args.name and not args.command:
        raise SystemExit("--name requires --command")
    if args.stage_plan and not args.command:
        raise SystemExit("--stage-plan requires --command")
    handled = _handle_nonlaunch_action(args)
    if handled is not None:
        return handled
    raw_cwd = Path(args.cwd).expanduser()
    if not raw_cwd.is_dir():
        raise SystemExit(f"Working directory does not exist or is not a directory: {raw_cwd}")
    cwd = raw_cwd.resolve(strict=True)
    if args.worker:
        return _run_owned_worker(args, cwd)
    return _launch_command(args, cwd)


def _handle_nonlaunch_action(args: argparse.Namespace) -> int | None:
    if args.list:
        print(json.dumps(list_runs(thread_id=_thread_id(), active_only=args.active), ensure_ascii=False))
        return 0
    if args.show:
        print(json.dumps(show_run(args.show), ensure_ascii=False))
        return 0
    if args.stop or args.detach:
        action_name = "stop" if args.stop else "detach"
        run_id = args.stop or args.detach
        assert isinstance(run_id, str)
        print(json.dumps(issue_control(run_id, action_name, thread_id=_thread_id()), ensure_ascii=False))
        return 0
    if args.adopt:
        print(json.dumps(adopt_run(args.adopt, thread_id=_thread_id(), codex_bin=args.codex_bin), ensure_ascii=False))
        return 0
    if args.adopt_worker:
        required = (args.spec_file, args.runtime_file, args.startup_file, args.gate_file)
        if not all(required) or not args.launcher_pid:
            raise SystemExit("adopt worker mode requires spec/runtime/startup/gate files and launcher pid")
        return run_adopted_worker(
            spec_file=Path(str(args.spec_file)).expanduser().resolve(),
            runtime_file=Path(str(args.runtime_file)).expanduser().resolve(),
            startup_file=Path(str(args.startup_file)).expanduser().resolve(),
            gate_file=Path(str(args.gate_file)).expanduser().resolve(),
            launcher_pid=args.launcher_pid,
            codex_bin=args.codex_bin,
            deliver_stage=deliver_stage_event,
            deliver_completion=deliver_completion,
        )
    if args.replay_pending:
        if args.monitor_plan:
            raise SystemExit("--monitor-plan cannot be combined with --replay-pending")
        log_dir = (
            Path(args.log_dir).expanduser().resolve()
            if args.log_dir
            else Path(args.cwd).expanduser().resolve() / ".codex-wake-run"
        )
        result = replay_pending(log_dir=log_dir, codex_bin=args.codex_bin)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result["status"] == "replay_complete" else 70
    return None


def _thread_id() -> str:
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        raise SystemExit("CODEX_THREAD_ID is missing; run wake-run from a Codex shell command.")
    return thread_id


def _optional_path(value: str | None) -> Path | None:
    return Path(value).expanduser().resolve() if value else None


def _run_owned_worker(args: argparse.Namespace, cwd: Path) -> int:
    if not args.thread_id or not args.log_file or not args.command:
        raise SystemExit("worker mode requires --thread-id, --log-file, and --command")
    return run_worker(
        thread_id=args.thread_id,
        command=args.command,
        cwd=cwd,
        log_file=Path(args.log_file).expanduser().resolve(),
        codex_bin=args.codex_bin,
        run_id=args.run_id,
        startup_file=_optional_path(args.startup_file),
        gate_file=_optional_path(args.gate_file),
        launcher_pid=args.launcher_pid,
        monitor_plan_file=_optional_path(args.monitor_plan_file),
        runtime_file=_optional_path(args.runtime_file),
        stage_plan_file=_optional_path(args.stage_plan_file),
    )


def _launch_command(args: argparse.Namespace, cwd: Path) -> int:
    log_dir = Path(args.log_dir).expanduser().resolve() if args.log_dir else cwd / ".codex-wake-run"
    monitor_policy = (
        load_monitor_policy(Path(args.monitor_plan).expanduser().resolve())
        if args.monitor_plan
        else None
    )
    result = arm_watcher(
        thread_id=_thread_id(),
        command=args.command,
        cwd=cwd,
        log_dir=log_dir,
        codex_bin=args.codex_bin,
        monitor_policy=monitor_policy,
        goal_policy=args.goal_policy,
        name=args.name,
        index_root=run_index_root(),
        stage_plan_source=(
            Path(args.stage_plan).expanduser().resolve() if args.stage_plan else None
        ),
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
