"""User-visible wake message formatting."""

from __future__ import annotations

from pathlib import Path

COMPLETION_HEADER = "[后台任务完成-系统提示]"
STAGE_HEADER = "[后台任务阶段-系统提示]"


def build_wake_message(
    command: str,
    exit_code: int | None,
    log_file: Path,
    *,
    duration_seconds: float | None = None,
    user_seconds: float | None = None,
    system_seconds: float | None = None,
    run_id: str = "",
    wake_id: str = "",
    monitor: dict[str, object] | None = None,
    terminal_state: str = "completed",
    observer_mode: str = "owned",
) -> str:
    lines = [
        COMPLETION_HEADER,
        f"任务：{command}",
        f"日志：{log_file}",
        f"exit_code: {exit_code}",
    ]
    lines.extend(
        f"{label}：{value:.3f}s"
        for label, value in (
            ("wall", duration_seconds),
            ("user", user_seconds),
            ("sys", system_seconds),
        )
        if value is not None
    )
    lines.extend([f"run_id：{run_id}", f"wake_id：{wake_id}"])
    if terminal_state != "completed":
        lines.append(f"state: {terminal_state}")
    if observer_mode == "adopted":
        lines.extend(["observer_mode: adopted", "exact_exit_code_available: false"])
    lines.extend(_monitor_message_lines(monitor))
    return "\n".join(lines)


def build_stage_message(event: dict[str, object]) -> str:
    return "\n".join([
        STAGE_HEADER,
        f"任务：{event['command']}",
        f"阶段：{event['stage_id']}",
        f"匹配日志：{event['matched_line']}",
        f"日志：{event['log_file']}",
        f"run_id：{event['run_id']}",
        f"wake_id：{event['wake_id']}",
        "terminal: false",
    ])


def _monitor_message_lines(monitor: dict[str, object] | None) -> list[str]:
    if not monitor or monitor.get("enabled") is not True:
        return []
    triage = monitor.get("triage")
    if not isinstance(triage, list):
        raise RuntimeError("Completion monitor triage must be an array")
    status = monitor.get("status")
    if not isinstance(status, str):
        status = "reviewed" if triage else "error" if monitor.get("error") else "unknown"
    lines = [f"monitor_status: {status}"]
    if triage and status == "reviewed":
        decision = triage[-1]
        if not isinstance(decision, dict):
            raise RuntimeError("Completion monitor decision must be an object")
        for field in ("action", "model", "failure_category", "summary", "reason"):
            value = decision.get(field)
            if not isinstance(value, str):
                raise RuntimeError(f"Completion monitor decision has no valid {field}")
            lines.append(f"monitor_{field}: {value}")
    error = monitor.get("error")
    if error is not None:
        if not isinstance(error, str):
            raise RuntimeError("Completion monitor error must be a string")
        lines.append(f"monitor_error: {error}")
    return lines
