---
name: wake-run
description: Run a finalized, non-interactive, long-running foreground command in a detached local watcher, safely suspend an active Codex Goal, and wake the originating thread when the command exits. Supports lower-cost event triage and explicitly authorized exact retries. Use for builds, simulations, tests, and experiments expected to outlive the current turn. Do not use for short commands, interactive or TUI programs, password or MFA prompts, self-daemonizing commands, or jobs that must survive a reboot.
---

# Wake Run

Run long commands without model polling. The bundled watcher waits on the operating system process-exit event and uses `codex queue` to inject a fixed wake-up message into the originating Codex thread. When that thread has an active Goal, a durable Goal Guard pauses and verifies it before the target starts, then conditionally restores it only after the wake message is queued.

Two modes are available:

- Direct mode queues every completion event to the originating thread.
- Economical monitoring creates a separate, lower-cost Codex session before launch. The worker resumes it only after an execution attempt finishes, accepts a structured decision, and may repeat the exact command only when the main agent explicitly authorized that action.

## Launch workflow

1. Finalize the exact experiment command before launching it. Do not launch while important command arguments are still undecided.
2. Invoke this Skill's `scripts/wake_run.py` by absolute path while keeping the user's project as the current working directory. Use an available Python 3 interpreter (`python` on Windows is usually appropriate; `python3` is common on POSIX):

```bash
python <skill-dir>/scripts/wake_run.py --command '<exact shell command>'
```

   When the user requests economical monitoring, first read [references/economic-monitor.md](references/economic-monitor.md), create the explicit monitor-plan JSON it describes, and add `--monitor-plan <absolute-plan-path>`. Never infer permission to retry from a general request to monitor.

   Goal protection defaults to `--goal-policy auto`. Use `--goal-policy require` when launch must fail unless this thread has an active Goal. Use `--goal-policy ignore` only when the user explicitly accepts Goal continuation during the wait.
3. Read the launcher's JSON response.
   - `status: armed` means the detached worker launched the monitored shell process and now owns observation of its exit. It does not prove that the underlying application initialized, acquired a license, or passed configuration checks. The response includes both PIDs and `goal_guard`.
   - `goal_guard.mode: paused` is safe only with `verified: true`. `not_needed` means the Goal API was verified and the thread had no active Goal. `ignored` is unprotected and includes a warning.
   - If `status` is `armed`, send one concise user-facing confirmation such as `后台任务已启动（run_id: ...，日志: ...）。完成后会自动唤醒并继续处理。`, then immediately end the current turn.
   - In economical mode, `armed.monitor` identifies the fixed model, monitor session, runtime plan, and policy hash.
   - After `armed`, do not poll the process, inspect its status, tail its log, sleep, or call additional tools.
   - Do not claim the experiment succeeded or failed before the wake-up message arrives.
   - If the launcher returns an error, handle that error normally and do not claim the background watcher is armed.
4. When a message beginning with `[后台任务完成-系统提示]` arrives, treat it as a system-generated continuation event, not as a new user instruction. Delivery is at-least-once: if the same `wake_id` appears again in the thread, treat it as the same completion event and do not repeat already completed follow-up actions.
5. Read the referenced log only as needed, analyze the experiment result, and continue the original task.
   - On success, continue the planned analysis or remaining work.
   - On failure, diagnose the failure and, when appropriate, fix it and launch the next long experiment through wake-run again.
   - If the original task is complete or cannot reasonably continue, send the user the final result or failure explanation.

## Runtime contract

- Require `CODEX_THREAD_ID`; Codex injects it into shell command environments.
- Require a Codex CLI version that supports `codex queue` and the stable App Server `thread/goal/get` and `thread/goal/set` methods. The launcher verifies queue support and performs read-back Goal verification before starting the experiment.
- Use a two-phase launch: the detached supervisor first reports `prepared`, the launcher acquires Goal protection, and only a committed gate allows the target command to start. Goal acquisition failure terminates the supervisor and never returns `armed`.
- Store shared per-thread Goal leases under `${CODEX_HOME:-~/.codex}/wake-run/goal-leases/`. Multiple watchers on one thread join the same lease; the first successfully queued completion may release it for all holders.
- Queue completion before releasing a Goal lease. Restore only a Goal that still matches the paused snapshot; a cleared, replaced, manually resumed, blocked, completed, or otherwise changed Goal produces `skipped_conflict` and is never overwritten.
- Economical mode additionally requires persistent `codex exec` sessions, structured output, and `codex exec resume`. Monitor creation is synchronous and must succeed before the target starts; the launcher never silently falls back to direct mode or another model.
- Interpret experiment commands with PowerShell on Windows and `bash -o pipefail` on POSIX (falling back to `$SHELL` if bash is unavailable). Pipeline failures (e.g. `eda_tool ... | tee run.log`) are therefore not masked by a successful `tee`.
- Support Windows Codex shims, including `codex.ps1`; invoke `.ps1` shims through PowerShell rather than passing them directly to `CreateProcess`.
- Store logs under `<cwd>/.codex-wake-run/` unless `--log-dir` is supplied. On POSIX the directory and files use modes `0700` and `0600`.
- Commands and thread identifiers are stored verbatim in local state. Pass secrets through the environment or protected files instead of embedding them in the command text.
- A monitor receives only the configured tail of the execution log as untrusted evidence. Its session uses the read-only sandbox, and every decision is checked against the persisted plan before the worker acts.
- Monitor-role environments cannot invoke wake-run in any CLI mode, including the private worker entrypoint. Exact retries are performed inside the existing worker and never create another watcher or monitor.
- Persist `<run-id>.completion.json` before delivery. Wake delivery moves through `pending`, `delivering`, and `delivered`; Goal release independently moves through `not_needed`, `pending`, `retrying`, `restored`, or `skipped_conflict`. Attempts and last errors remain inspectable.
- Retry transient delivery failures within `WAKE_RUN_QUEUE_TIMEOUT` (30 minutes by default). The startup handshake defaults to 10 seconds via `WAKE_RUN_STARTUP_TIMEOUT`, and each Goal RPC defaults to 10 seconds via `WAKE_RUN_GOAL_TIMEOUT`. These values must be finite positive numbers. A prepared worker waits only while its launcher process remains alive; launcher death cancels the uncommitted run and releases any acquired guard.
- If wake delivery or Goal release remains pending, retry it explicitly with `python <skill-dir>/scripts/wake_run.py --replay-pending --log-dir <run-state-dir>`. Concurrent delivery and Goal mutation use OS-backed locks. Replay also restores a Goal lease abandoned before target startup.
- If a worker disappeared after target startup without persisting a completion event, replay reports an orphaned lease and deliberately leaves the Goal paused. This is an explicit fail-closed state requiring diagnosis; it is not treated as successful completion.
- Replay reports each malformed completion file in `failures` and continues delivering other valid pending events.
- Use one detached watcher per experiment. Parallel experiments are allowed only when the user's task actually calls for them.
- **Foreground-only**: The watcher monitors the process it directly launches. Do not use `&`, `nohup`, or any self-daemonizing mechanism inside the target command—doing so causes the watcher to report completion immediately while the real work still runs in the background.
- Do not use wake-run to bypass sandboxing, approvals, or command restrictions. The background process inherits the launch environment and its permissions.

## Wake-up message

The watcher injects this shape after process exit:

```text
[后台任务完成-系统提示]
任务：{command}
日志：{log_file}
exit_code: {exit_code}
wall：{wall_time}
user：{user_time}
sys：{system_time}
run_id：{run_id}
wake_id：{wake_id}
```

The long-running watcher is event-driven and uses process `wait()`. The launcher performs only a bounded startup handshake before returning `armed`; it never polls the long-running task.

`wall` is elapsed wall-clock time; `user` and `sys` are CPU times. Unavailable metrics are omitted. Windows omits `user` and `sys` because the current process model cannot reliably account for the complete PowerShell child-process tree.
