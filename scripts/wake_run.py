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

from wake_run_core import arm_watcher, replay_pending, run_worker  # noqa: E402
from wake_run_monitor import load_monitor_policy, reject_recursive_launch  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a detached command and wake the originating Codex thread on exit."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--command", help="Exact shell command to execute.")
    action.add_argument("--replay-pending", action="store_true", help="Retry undelivered completion events.")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for the command.")
    parser.add_argument("--log-dir", help="Run state directory; defaults to <cwd>/.codex-wake-run.")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable.")
    parser.add_argument(
        "--monitor-plan",
        help="Enable economical monitoring with an explicit JSON authorization plan.",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--thread-id", help=argparse.SUPPRESS)
    parser.add_argument("--log-file", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--startup-file", help=argparse.SUPPRESS)
    parser.add_argument("--monitor-plan-file", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cwd = Path(args.cwd).expanduser().resolve()
    log_dir = Path(args.log_dir).expanduser().resolve() if args.log_dir else cwd / ".codex-wake-run"
    if not args.worker:
        reject_recursive_launch()
    if args.replay_pending:
        if args.monitor_plan:
            raise SystemExit("--monitor-plan cannot be combined with --replay-pending")
        result = replay_pending(log_dir=log_dir, codex_bin=args.codex_bin)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result["status"] == "replay_complete" else 70
    if args.worker:
        if not args.thread_id or not args.log_file or not args.command:
            raise SystemExit("worker mode requires --thread-id, --log-file, and --command")
        startup_file = Path(args.startup_file).expanduser().resolve() if args.startup_file else None
        monitor_plan_file = (
            Path(args.monitor_plan_file).expanduser().resolve() if args.monitor_plan_file else None
        )
        return run_worker(
            thread_id=args.thread_id,
            command=args.command,
            cwd=cwd,
            log_file=Path(args.log_file).expanduser().resolve(),
            codex_bin=args.codex_bin,
            run_id=args.run_id,
            startup_file=startup_file,
            monitor_plan_file=monitor_plan_file,
        )
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        raise SystemExit("CODEX_THREAD_ID is missing; run wake-run from a Codex shell command.")
    monitor_policy = (
        load_monitor_policy(Path(args.monitor_plan).expanduser().resolve())
        if args.monitor_plan
        else None
    )
    result = arm_watcher(
        thread_id=thread_id,
        command=args.command,
        cwd=cwd,
        log_dir=log_dir,
        codex_bin=args.codex_bin,
        monitor_policy=monitor_policy,
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
