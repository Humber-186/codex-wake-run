---
name: wake-run
description: Run, inspect, stop, detach, or recover finalized non-interactive long commands in detached local watchers, safely suspend an active Codex Goal, and wake the originating thread at ordered log milestones or process termination. Supports persistent run discovery, lower-cost terminal triage, and explicitly authorized exact retries. Use for builds, EDA flows, simulations, tests, and experiments expected to outlive the current turn, or when the user asks about runs previously started by wake-run. Do not use for short commands, interactive or TUI programs, password or MFA prompts, self-daemonizing commands, or jobs that must survive a reboot.
---

# Wake Run

Run long commands without model polling. The bundled watcher waits on the operating system process-exit event and uses `codex queue` to inject a fixed wake-up message into the originating Codex thread. When that thread has an active Goal, a durable Goal Guard pauses and verifies it before the target starts, then conditionally restores it only after the wake message is queued.

Two modes are available:

- Direct mode queues every completion event to the originating thread.
- Economical monitoring persists an explicit policy before launch and lazily creates a lower-cost Codex session only when `review_on` selects an execution result. It accepts a structured decision and may repeat the exact command only when the main agent explicitly authorized that action.

## Launch workflow

1. Finalize the exact experiment command before launching it. Do not launch while important command arguments are still undecided.
2. Invoke this Skill's `scripts/wake_run.py` by absolute path while keeping the user's project as the current working directory. Use an available Python 3 interpreter (`python` on Windows is usually appropriate; `python3` is common on POSIX):

```bash
python <skill-dir>/scripts/wake_run.py --command '<exact shell command>'
```

   When the user requests economical monitoring, first read [references/economic-monitor.md](references/economic-monitor.md), create the explicit monitor-plan JSON it describes, and add `--monitor-plan <absolute-plan-path>`. Never infer permission to retry from a general request to monitor.

   Goal protection defaults to `--goal-policy auto`. Use `--goal-policy require` when launch must fail unless this thread has an active Goal. Use `--goal-policy ignore` only when the user explicitly accepts Goal continuation during the wait.

   For a single process with known log milestones, create a strict stage-plan JSON and pass `--stage-plan <absolute-path>`:

```json
{"schema_version":1,"stages":[{"id":"dc","pattern":"DC DONE"},{"id":"route","pattern":"ROUTE DONE"}]}
```

   Stages are ordered, one-shot `log_line_regex` events. Use patterns emitted by the actual foreground process; do not infer ambiguous progress rules.
3. Read the launcher's JSON response.
   - `status: armed` means the detached worker launched the monitored shell process and now owns observation of its exit. It does not prove that the underlying application initialized, acquired a license, or passed configuration checks. The response includes both PIDs and `goal_guard`.
   - `goal_guard.mode: paused` is protected only with `verified: true`. It also reports `runtime_scope: detached` and `current_turn_accounting: not_guaranteed`; Goal continuation is stopped, but the launching turn's Goal usage may be omitted. `not_needed` means the Goal API was verified and the thread had no active Goal. `ignored` is unprotected and includes a warning.
   - If `status` is `armed`, send one concise user-facing confirmation such as `后台任务已启动（run_id: ...，日志: ...）。完成后会自动唤醒并继续处理。`, then immediately end the current turn.
   - Add `--name <short-name>` when a human-readable label will help later discovery.
   - In economical mode, `armed.monitor` identifies the fixed model, review scope, lazy session state, runtime plan, and policy hash. `session_id` is null until the first selected event.
   - After `armed`, do not autonomously poll the process, inspect its status, tail its log, sleep, or call additional tools. If the user explicitly asks about running jobs, make one deterministic query with `--list --active` or `--show <run_id>`.
   - Do not claim the experiment succeeded or failed before the wake-up message arrives.
   - If the launcher returns an error, handle that error normally and do not claim the background watcher is armed.
4. Treat messages beginning with `[后台任务阶段-系统提示]` or `[后台任务完成-系统提示]` as system-generated continuation events, not new user instructions. Delivery is at-least-once: the same `wake_id` is the same event. A stage message has `terminal: false`; inspect or act as needed, then leave the still-running Run under supervision. A completion, stop, or explicit detach closes supervision; stage events do not release the Goal holder.
5. Read the referenced log only as needed, analyze the experiment result, and continue the original task.
   - On success, continue the planned analysis or remaining work.
   - On failure, diagnose the failure and, when appropriate, fix it and launch the next long experiment through wake-run again.
   - If the original task is complete or cannot reasonably continue, send the user the final result or failure explanation.

## Run discovery

- Use `python <skill-dir>/scripts/wake_run.py --list --active` for a user-requested snapshot of active runs owned by the current thread.
- Use `python <skill-dir>/scripts/wake_run.py --show <run_id>` for one known run. Report verified state and liveness fields without inventing progress percentages.

## Lifecycle control

- `--stop <run_id>` terminates the owned target process tree, persists a `cancelled` terminal event, and wakes the thread. Use it only when the user asked to stop/cancel the run or that action is otherwise already authorized.
- `--detach <run_id>` releases this Run's Goal holder and ends supervision while leaving the target alive. It deliberately gives up the exact exit code and final notification.
- `--adopt <run_id>` is a Linux recovery operation for a dead worker and a still-live, identity-verified target. It resumes stage and terminal observation. The adopted observer cannot recover the original parent's wait status, so report `exact_exit_code_available: false`; never describe an adopted exit as success or failure from its exit code.
- These actions are scoped to Runs owned by the current `CODEX_THREAD_ID`. A healthy worker cannot be adopted; a dead worker cannot acknowledge stop/detach until it is adopted.

## Runtime contract

- Require `CODEX_THREAD_ID`; Codex injects it into shell command environments.
- The stable support baseline is Linux with Codex CLI 0.154.0 or newer. The launcher verifies `codex queue` and performs read-back verification through App Server `thread/goal/get` and `thread/goal/set` before starting the experiment.
- Use a two-phase launch: the detached supervisor first reports `prepared`, the launcher acquires Goal protection, and only a committed gate allows the target command to start. Goal acquisition failure terminates the supervisor and never returns `armed`.
- Store shared per-thread Goal leases under `${CODEX_HOME:-~/.codex}/wake-run/goal-leases/`. Multiple watchers on one thread join the same lease; the first successfully queued completion may release it only when no holder is orphaned.
- Store a small global run index under `${CODEX_HOME:-~/.codex}/wake-run/index/`; immutable spec and mutable runtime files remain beside each run log. `--list` is scoped to the current Codex thread, while `--show <run_id>` resolves one known run.
- Persist stage events before queue delivery. Match complete log lines incrementally and only against the next unfinished stage. Stage events never release Goal state; replay covers both pending stage and completion events.
- Treat `detach` as the only way to intentionally abandon terminal observation. `adopt` verifies Linux boot ID and process starttime to reject PID reuse, and remains explicitly unable to provide an exact target exit code.
- Queue completion before releasing a Goal lease. Restore only a Goal that still matches the paused snapshot; a cleared, replaced, manually resumed, blocked, completed, or otherwise changed Goal produces `skipped_conflict` and is never overwritten.
- Economical mode additionally requires persistent `codex exec` sessions, structured output, and `codex exec resume`. The launcher preflights those commands before target start; actual session creation is lazy and any invocation failure is persisted and delivered, never silently replaced by direct mode or another model.
- Interpret experiment commands with PowerShell on Windows and `bash -o pipefail` on POSIX (falling back to `$SHELL` if bash is unavailable). Pipeline failures (e.g. `eda_tool ... | tee run.log`) are therefore not masked by a successful `tee`.
- Support Windows Codex shims, including `codex.ps1`; invoke `.ps1` shims through PowerShell rather than passing them directly to `CreateProcess`.
- Store logs under `<cwd>/.codex-wake-run/` unless `--log-dir` is supplied. On POSIX the directory and files use modes `0700` and `0600`.
- Commands and thread identifiers are stored verbatim in local state. Pass secrets through the environment or protected files instead of embedding them in the command text.
- A monitor receives only the configured tail of the execution log as untrusted evidence. Its session uses the read-only sandbox, and every decision is checked against the persisted plan before the worker acts.
- Monitor-role environments cannot invoke wake-run in any CLI mode, including the private worker entrypoint. Exact retries are performed inside the existing worker and never create another watcher or monitor.
- Persist `<run-id>.completion.json` before delivery. Wake delivery moves through `pending`, `delivering`, and `delivered`; Goal release independently moves through `not_needed`, `pending`, `retrying`, `restored`, or `skipped_conflict`. Attempts and last errors remain inspectable.
- Retry transient delivery failures within `WAKE_RUN_QUEUE_TIMEOUT` (30 minutes by default). The startup handshake defaults to 10 seconds via `WAKE_RUN_STARTUP_TIMEOUT`, and each Goal RPC defaults to 10 seconds via `WAKE_RUN_GOAL_TIMEOUT`. These values must be finite positive numbers. A prepared worker waits only while its launcher process remains alive; launcher death cancels the uncommitted run and releases any acquired guard.
- If wake delivery or Goal release remains pending, retry it explicitly with `python <skill-dir>/scripts/wake_run.py --replay-pending --log-dir <run-state-dir>`. Delivery locks remain non-blocking; Goal mutations use a blocking OS lock so normal short contention is serialized. Replay also restores a Goal lease abandoned while still `prepared`.
- Before `Popen`, persist the holder as `spawning`; after success persist `running` and the target PID. If a worker disappears in either phase without a completion event, persist the whole lease as `orphaned`, reject new holders and ordinary restoration, and leave the Goal paused.
- Goal mutation uses a separate stdio App Server. Codex CLI 0.154.0 exposes no current-TUI live Goal runtime endpoint to a Skill, so the launching turn's `tokensUsed`, `timeUsedSeconds`, and budget enforcement are not guaranteed. Do not describe detached Goal protection as complete Goal accounting.
- The public Goal API has no atomic compare-and-swap parameter. Snapshot checks prevent ordinary overwrites but cannot eliminate the narrow external-mutation window between read and update.
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

For a declared milestone it injects:

```text
[后台任务阶段-系统提示]
任务：{command}
阶段：{stage_id}
匹配日志：{matched_line}
日志：{log_file}
run_id：{run_id}
wake_id：{wake_id}
terminal: false
```

The target waiter uses `process.wait()`. When stages are configured, the supervisor incrementally checks the local log and durable control directory; the launcher itself performs only a bounded startup handshake before returning `armed` and never polls the task.

`wall` is elapsed wall-clock time; `user` and `sys` are CPU times. Unavailable metrics are omitted. Windows omits `user` and `sys` because the current process model cannot reliably account for the complete PowerShell child-process tree.
