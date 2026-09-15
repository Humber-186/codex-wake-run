<h1 align="center">wake-run-skill</h1>

<p align="center">A Codex Skill that runs long commands in a detached watcher and wakes the originating Codex thread when the process exits, so the model never polls for status.</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square" alt="Python 3.12"> <a href="https://linux.do/latest"><img src="https://img.shields.io/badge/Linux.do-Community-7C3AED?style=flat-square" alt="Linux.do community"></a>
</p>

Long experiments create an awkward choice inside an agent session: either the model sits in a polling loop burning turns while it waits, or you lose the thread of the original task and have to re-explain it later.

wake-run removes the wait. You hand it the finalized command; it spawns a detached watcher and performs a short startup handshake. It prints `status: armed` only after the monitored shell process starts and the watcher owns its eventual exit status, then the current turn ends. This does not prove that the underlying application initialized, acquired a license, or passed configuration checks. The watcher blocks on the operating system process-exit event. When the command finishes, succeeds or fails, the watcher persists a completion event and uses `codex queue` to inject a wake-up message back into the same thread. You can also explicitly enable economical monitoring so a lower-cost model such as Luna triages execution events and, within narrow authorization, retries the exact same command.

## Highlights

| Highlight | Why it matters |
|---|---|
| Event-driven long jobs | The watcher blocks on `process.wait()`; only the bounded startup handshake checks a status file. |
| Strict startup confirmation | `armed` is returned only after the worker reports the monitored shell PID. Startup failure and timeout are explicit errors. |
| Wakes the same thread | The watcher calls `codex queue --thread "$CODEX_THREAD_ID"`, so the continuation lands in the conversation that started the job. |
| Failures stay visible | Non-zero command exits wake the thread; worker or target startup failures fail synchronously before `armed`. |
| Windows and POSIX paths | Commands run through PowerShell on Windows and `bash -o pipefail` on POSIX, including `codex.ps1` shim handling. CI defines both platforms; release confidence depends on actual Actions results. |
| Durable delivery state | Each run has a log and atomic completion JSON recording delivery attempts, errors, and final state. |
| Optional economical monitoring | A separate lower-cost Codex session performs event-time triage; the main agent explicitly fixes the model, evidence budget, and exact-retry authorization. |
| Structural recursion prevention | Monitor-role processes cannot launch or replay wake-run; retries stay inside the existing worker and never create nested watchers. |

## Architecture

```text
Codex thread
    │
    │ launch wake-run
    ▼
wake_run.py launcher
    │
    │ detached worker + target PID handshake
    ▼
experiment process
    │
    │ process.wait()
    ▼
exit code + log
    │
    ├─ direct mode: durable completion event
    │
    └─ economical mode: resume read-only monitor session
            ├─ authorized retry_exact ──► run exact command again
            └─ success / escalation / monitor error
    │
    │ codex queue
    ▼
Codex thread wakes and continues
```

The launcher verifies `codex queue` support before it starts anything, so an incompatible Codex CLI fails fast instead of running your job and then failing to wake anyone.

## Usage Example

**You:**

```text
Use the wake-run skill to run this experiment and continue after it exits.
```

**Codex** launches the job and gets `armed` back:

```json
{"status": "armed", "run_id": "b7599ab35869", "worker_pid": 97153, "process_pid": 97154, "log_file": "/work/project/.codex-wake-run/b7599ab35869.log"}
```

It then ends the turn. Nothing polls the job.

When the script exits, the watcher queues a wake-up into that same thread:

```text
[后台任务唤醒通知]

脚本：echo "training started"; sleep 2; echo "done"
状态：执行完成
退出码：0
日志文件：/work/project/.codex-wake-run/b7599ab35869.log
run_id：b7599ab35869
wake_id：5e9ca210a8c84d9d97b66a9ec0a79d58

请分析脚本执行结果，然后继续完成原任务。
若任务已经完成，请直接向用户发送最终结果。
若脚本执行失败，请分析失败原因，并在合理情况下修复后继续执行。

注：该消息由系统后台唤醒，并非用户亲自发出消息。
```

After receiving `armed`, Codex sends one concise confirmation and ends the current turn. It reads the referenced log only after the wake-up arrives, then continues the original task.

Wake delivery is at-least-once. Retries reuse the same `wake_id`, so duplicate messages represent the same completion event and must not repeat completed follow-up work. Before each delivery attempt, `<run_id>.completion.json` records `pending`, `delivering`, or `delivered` plus attempt details. Undelivered events can be retried explicitly:

```bash
python <skill-dir>/scripts/wake_run.py --replay-pending --log-dir <run-state-dir>
```

## Economical Monitoring

Economical monitoring is not model polling. The operating-system watcher still waits in `process.wait()`; the monitor model is called only after an execution attempt ends. The main agent first writes a strict JSON plan:

```json
{
  "schema_version": 1,
  "model": "gpt-5.6-luna",
  "instructions": "Summarize success. Retry only a clear transient external service failure; escalate every other failure.",
  "allowed_actions": ["retry_exact"],
  "max_exact_retries": 1,
  "log_tail_bytes": 65536
}
```

Then enable it explicitly:

```bash
python3 <skill-dir>/scripts/wake_run.py \
  --command '<finalized exact command>' \
  --monitor-plan '<absolute path to plan JSON>'
```

Before starting the target, the launcher creates and confirms a separate read-only Codex session. Every resume explicitly reapplies the read-only sandbox, fixed state-directory cwd, and non-Git-directory allowance. Failure is explicit: it never falls back to direct mode or substitutes another model. The monitor must return `report_success`, `retry_exact`, or `escalate`. The worker validates run identity, the plan hash, exit state, failure classification, and remaining authorization; the model cannot supply a modified command, and launch or wait errors cannot be retried automatically. Monitor invocation or protocol failure is persisted in the completion event and wakes the main thread.

Authorize `retry_exact` only when repeating the complete command is safe even if the prior attempt produced partial side effects. A transient external failure does not prove that no side effect occurred; deployment, publishing, payment, and database-migration commands should normally disallow automatic retries.

This mode currently watches process-exit events only; it does not claim to detect a hung process. See [`references/economic-monitor.md`](./references/economic-monitor.md) for the complete plan and decision contract.

## Quick Start

This repository is itself a standalone Skill. There is no plugin manifest or nested Skill directory.

Ask Codex to install it:

```text
Install the wake-run skill for me: https://github.com/Humber-186/codex-wake-run
```

Once installed, use it like this:

```text
Use the wake-run skill to run xxx for me.
```

A few things worth knowing:

- **It only works inside a Codex session.** The Skill needs `CODEX_THREAD_ID` to know which thread to wake.
- **Your Codex CLI needs `codex queue`.** The launcher preflights this before starting the experiment.
- **Economical monitoring also needs persistent `codex exec` sessions, structured output, and `codex exec resume`.** Its explicit `WAKE_RUN_MONITOR_TIMEOUT` defaults to 300 seconds.
- **It is not a way around your sandbox.** The background process inherits the launch environment and its permissions.
- **One watcher per experiment.** Multiple watchers are fine when the task genuinely requires parallel jobs.

## Repository layout

| Path | What it holds |
|---|---|
| [`SKILL.md`](./SKILL.md) | Skill instructions: launch workflow, runtime contract, and wake-up message shape. |
| [`scripts/wake_run.py`](./scripts/wake_run.py) | Command-line entrypoint. |
| [`scripts/wake_run_core.py`](./scripts/wake_run_core.py) | Startup handshake, wake delivery, and replay. |
| [`scripts/wake_run_monitor.py`](./scripts/wake_run_monitor.py) | Monitor plans, Codex sessions, structured triage, and recursion prevention. |
| [`scripts/wake_run_worker.py`](./scripts/wake_run_worker.py) | Target-process execution and completion persistence. |
| [`scripts/wake_run_process.py`](./scripts/wake_run_process.py) | Cross-platform target process groups and failure cleanup. |
| [`scripts/wake_run_state.py`](./scripts/wake_run_state.py) | Atomic state persistence, permissions, and delivery locking. |
| [`scripts/wake_run_platform.py`](./scripts/wake_run_platform.py) | Windows and POSIX command construction. |
| [`agents/openai.yaml`](./agents/openai.yaml) | Agent-facing Skill metadata; implicit invocation is enabled. |
| [`tests/test_wake_run.py`](./tests/test_wake_run.py) | Unit, integration, Windows, and regression tests. |
| [`tests/test_recovery.py`](./tests/test_recovery.py) | Recovery, persistence-failure, and replay tests. |
| [`tests/test_monitor.py`](./tests/test_monitor.py) | Economical-monitor plans, protocol, authorized retries, and end-to-end tests. |
| [`tests/test_process.py`](./tests/test_process.py) | POSIX and Windows process-tree cleanup tests. |
| [`references/economic-monitor.md`](./references/economic-monitor.md) | Economical-monitor plan and decision contract. |

Run logs default to `.codex-wake-run/` in the project you launch from. This repository ignores that directory, but a host project does not inherit this repository's `.gitignore`; add `.codex-wake-run/` to the host project yourself. For large EDA projects, `--log-dir ~/.codex/wake-run/<project>` keeps frequently updated state off the source tree or NFS storage.

## Acknowledgements

Thanks to the [Linux.do](https://linux.do/latest) community for its support.
