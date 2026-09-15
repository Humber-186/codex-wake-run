---
name: wake-run
description: Run a finalized, non-interactive, long-running foreground command in a detached local watcher and wake the originating Codex thread when it exits, optionally with lower-cost event triage and explicitly authorized exact retries. Use for builds, simulations, tests, and experiments expected to outlive the current turn. Do not use for short commands, interactive or TUI programs, password or MFA prompts, self-daemonizing commands, or jobs that must survive a reboot.
---

# Wake Run

Run long commands without model polling. The bundled watcher waits on the operating system process-exit event and uses `codex queue` to inject a fixed wake-up message into the originating Codex thread.

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

3. Read the launcher's JSON response.
   - `status: armed` means the detached worker launched the monitored shell process and now owns observation of its exit. It does not prove that the underlying application initialized, acquired a license, or passed configuration checks. The response includes both PIDs.
   - If `status` is `armed`, send one concise user-facing confirmation such as `后台任务已启动（run_id: ...，日志: ...）。完成后会自动唤醒并继续处理。`, then immediately end the current turn.
   - In economical mode, `armed.monitor` identifies the fixed model, monitor session, runtime plan, and policy hash.
   - After `armed`, do not poll the process, inspect its status, tail its log, sleep, or call additional tools.
   - Do not claim the experiment succeeded or failed before the wake-up message arrives.
   - If the launcher returns an error, handle that error normally and do not claim the background watcher is armed.
4. When a message beginning with `[后台任务唤醒通知]` arrives, treat it as a system-generated continuation event, not as a new user instruction. Delivery is at-least-once: if the same `wake_id` appears again in the thread, treat it as the same completion event and do not repeat already completed follow-up actions.
5. Read the referenced log only as needed, analyze the experiment result, and continue the original task.
   - On success, continue the planned analysis or remaining work.
   - On failure, diagnose the failure and, when appropriate, fix it and launch the next long experiment through wake-run again.
   - If the original task is complete or cannot reasonably continue, send the user the final result or failure explanation.

## Runtime contract

- Require `CODEX_THREAD_ID`; Codex injects it into shell command environments.
- Require a Codex CLI version that supports `codex queue`. The launcher verifies this before starting the experiment.
- Economical mode additionally requires persistent `codex exec` sessions, structured output, and `codex exec resume`. Monitor creation is synchronous and must succeed before the target starts; the launcher never silently falls back to direct mode or another model.
- Interpret experiment commands with PowerShell on Windows and `bash -o pipefail` on POSIX (falling back to `$SHELL` if bash is unavailable). Pipeline failures (e.g. `eda_tool ... | tee run.log`) are therefore not masked by a successful `tee`.
- Support Windows Codex shims, including `codex.ps1`; invoke `.ps1` shims through PowerShell rather than passing them directly to `CreateProcess`.
- Store logs under `<cwd>/.codex-wake-run/` unless `--log-dir` is supplied. On POSIX the directory and files use modes `0700` and `0600`.
- Commands and thread identifiers are stored verbatim in local state. Pass secrets through the environment or protected files instead of embedding them in the command text.
- A monitor receives only the configured tail of the execution log as untrusted evidence. Its session uses the read-only sandbox, and every decision is checked against the persisted plan before the worker acts.
- Monitor-role environments cannot invoke wake-run in any CLI mode, including the private worker entrypoint. Exact retries are performed inside the existing worker and never create another watcher or monitor.
- Persist `<run-id>.completion.json` before delivery. Its delivery state moves through `pending`, `delivering`, and `delivered`; delivery attempts and the last error remain inspectable.
- Retry transient delivery failures within `WAKE_RUN_QUEUE_TIMEOUT` (30 minutes by default). The startup handshake defaults to 10 seconds and can be configured with `WAKE_RUN_STARTUP_TIMEOUT`. Both values must be finite positive numbers.
- If an event remains undelivered, retry it explicitly with `python <skill-dir>/scripts/wake_run.py --replay-pending --log-dir <run-state-dir>`. Concurrent delivery of the same event is locked; a replay reports `replay_incomplete` while another live process owns it.
- Replay reports each malformed completion file in `failures` and continues delivering other valid pending events.
- Use one detached watcher per experiment. Parallel experiments are allowed only when the user's task actually calls for them.
- **Foreground-only**: The watcher monitors the process it directly launches. Do not use `&`, `nohup`, or any self-daemonizing mechanism inside the target command—doing so causes the watcher to report completion immediately while the real work still runs in the background.
- Do not use wake-run to bypass sandboxing, approvals, or command restrictions. The background process inherits the launch environment and its permissions.

## Wake-up message

The watcher injects this shape after process exit:

```text
[后台任务唤醒通知]

脚本：{command}
状态：{执行完成|执行失败}
退出码：{exit_code}
日志文件：{log_path}
run_id：{run_id}
wake_id：{wake_id}

请分析脚本执行结果，然后继续完成原任务。
若任务已经完成，请直接向用户发送最终结果。
若脚本执行失败，请分析失败原因，并在合理情况下修复后继续执行。

注：该消息由系统后台唤醒，并非用户亲自发出消息。
```

The long-running watcher is event-driven and uses process `wait()`. The launcher performs only a bounded startup handshake before returning `armed`; it never polls the long-running task.
