"""Validated, ordered log milestones for a wake-run process."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from wake_run_state import atomic_write_json, read_json

STAGE_SCHEMA_VERSION = 1
STAGE_PLAN_FIELDS = frozenset({"schema_version", "stages"})
STAGE_FIELDS = frozenset({"id", "pattern"})
STAGE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True)
class StageRule:
    stage_id: str
    pattern: str

    def matches(self, line: str) -> bool:
        return re.search(self.pattern, line) is not None


@dataclass(frozen=True)
class StageMatch:
    stage_id: str
    line: str
    log_offset: int


def load_stage_plan(path: Path) -> tuple[StageRule, ...]:
    payload = read_json(path)
    if set(payload) != STAGE_PLAN_FIELDS or payload.get("schema_version") != STAGE_SCHEMA_VERSION:
        raise RuntimeError("Stage plan must contain only schema_version: 1 and stages")
    values = payload.get("stages")
    if not isinstance(values, list) or not values:
        raise RuntimeError("Stage plan stages must be a non-empty array")
    rules = tuple(_parse_rule(value) for value in values)
    ids = [rule.stage_id for rule in rules]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Stage ids must be unique")
    return rules


def persist_stage_plan(source: Path, destination: Path) -> tuple[StageRule, ...]:
    rules = load_stage_plan(source)
    atomic_write_json(destination, {
        "schema_version": STAGE_SCHEMA_VERSION,
        "stages": [{"id": rule.stage_id, "pattern": rule.pattern} for rule in rules],
    })
    return rules


def _parse_rule(value: object) -> StageRule:
    if not isinstance(value, dict) or set(value) != STAGE_FIELDS:
        raise RuntimeError("Each stage must contain only id and pattern")
    stage_id = value.get("id")
    pattern = value.get("pattern")
    if not isinstance(stage_id, str) or not stage_id.strip():
        raise RuntimeError("Stage id must be a non-empty string")
    if STAGE_ID_PATTERN.fullmatch(stage_id) is None:
        raise RuntimeError(f"Stage id {stage_id!r} contains unsupported characters")
    if not isinstance(pattern, str) or not pattern:
        raise RuntimeError(f"Stage {stage_id!r} pattern must be a non-empty string")
    try:
        re.compile(pattern)
    except re.error as error:
        raise RuntimeError(f"Stage {stage_id!r} has invalid regex: {error}") from error
    return StageRule(stage_id=stage_id, pattern=pattern)


class StageScanner:
    """Scan complete log lines and trigger only the next uncompleted rule."""

    def __init__(
        self,
        rules: tuple[StageRule, ...],
        *,
        completed: tuple[str, ...] = (),
        offset: int = 0,
    ) -> None:
        expected = tuple(rule.stage_id for rule in rules[: len(completed)])
        if completed != expected:
            raise RuntimeError("Completed stages are not an ordered prefix of the stage plan")
        if offset < 0:
            raise RuntimeError("Stage log offset must not be negative")
        self.rules = rules
        self.completed = list(completed)
        self.offset = offset

    def scan(self, log_file: Path, *, final: bool = False) -> tuple[StageMatch, ...]:
        data = self._read_new_bytes(log_file)
        complete, consumed = _complete_lines(data, final=final)
        matches: list[StageMatch] = []
        cursor = self.offset
        for raw_line in complete:
            cursor += len(raw_line)
            while len(self.completed) < len(self.rules):
                rule = self.rules[len(self.completed)]
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not rule.matches(line):
                    break
                self.completed.append(rule.stage_id)
                matches.append(StageMatch(rule.stage_id, line, cursor))
        self.offset += consumed
        return tuple(matches)

    def _read_new_bytes(self, log_file: Path) -> bytes:
        with log_file.open("rb") as stream:
            stream.seek(self.offset)
            return stream.read()


def _complete_lines(data: bytes, *, final: bool) -> tuple[list[bytes], int]:
    lines = data.splitlines(keepends=True)
    if not lines:
        return [], 0
    if not final and not lines[-1].endswith((b"\n", b"\r")):
        lines.pop()
    return lines, sum(len(line) for line in lines)
