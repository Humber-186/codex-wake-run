# Economical Monitoring

Use this mode only when the user asks for a lower-cost model to triage a long task. It does not reduce idle waiting cost: the operating-system watcher already waits without model calls. It reduces main-agent work after execution events and can absorb an explicitly authorized exact-command retry.

## Prepare the plan

Create a JSON file with exactly these fields:

```json
{
  "schema_version": 1,
  "model": "gpt-5.6-luna",
  "instructions": "Summarize success. Retry only a clearly transient external service failure. Escalate every task, code, configuration, dependency, permission, repeated, or ambiguous failure.",
  "allowed_actions": ["retry_exact"],
  "max_exact_retries": 1,
  "log_tail_bytes": 65536
}
```

- Preserve the model requested by the user. `gpt-5.6-luna` is the usual economical choice, not a silent substitute for another requested model.
- Make `instructions` task-specific enough to distinguish a known transient external failure from a task failure.
- `allowed_actions` currently accepts only `retry_exact`. Use an empty array when the monitor is read-only.
- Set `max_exact_retries` to `0` unless `retry_exact` is present. Any positive value is an explicit authorization boundary chosen for this task.
- `log_tail_bytes` is the exact evidence budget supplied to the monitor. The log is marked as untrusted data.

Then launch:

```bash
python3 <skill-dir>/scripts/wake_run.py \
  --command '<exact shell command>' \
  --monitor-plan '<absolute-plan-path>'
```

## Decision contract

The monitor runs in a separate persistent Codex session with the configured model and a read-only sandbox. It is invoked only after an execution attempt and must return one structured action:

- `report_success`: valid only for exit code zero with no execution error.
- `retry_exact`: valid only for a process that completed with a nonzero exit code and was classified as `transient_external`, when the plan authorizes it and a retry remains. Launch and wait errors are always escalated because the prior process state may be uncertain.
- `escalate`: required for complex, ambiguous, repeated, task, code, configuration, dependency, or permission failures.

The worker validates the action, classification, success state, remaining authorization, run identity, and runtime-plan hash. It never accepts a modified command from the model. Invalid output or monitor failure is recorded explicitly and the originating thread is awakened.

## Recursion boundary

Monitor calls carry `WAKE_RUN_ROLE=monitor`, the root run ID, and monitor depth `1`. Public launch and replay operations reject that role or any positive monitor depth. A retry is an internal new attempt of the same command and run; it does not invoke the Skill, create a watcher, or create another monitor.

Completion state records every execution attempt, monitor decision, model, session ID, policy hash, and monitor error. Delivery remains at-least-once under the original `wake_id`; duplicate wake messages must not repeat follow-up work.
