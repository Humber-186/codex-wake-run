"""Durable stage-event commits shared by owned and adopted observers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from wake_run_events import create_stage_event, stage_event_path
from wake_run_registry import write_run_runtime
from wake_run_stages import StageMatch, StageScanner

CHECKPOINT_INTERVAL_SECONDS = 5.0
CHECKPOINT_BYTE_INTERVAL = 1024 * 1024
StageSubmit = Callable[[Path], None]


@dataclass(frozen=True)
class StageCommitContext:
    run_id: str
    thread_id: str
    command: str
    log_file: Path
    runtime_file: Path | None


class StageCommitter:
    """Persist stage facts before advancing their runtime checkpoint."""

    def __init__(
        self,
        context: StageCommitContext,
        scanner: StageScanner | None,
        submit: StageSubmit,
    ) -> None:
        self._context = context
        self._scanner = scanner
        self._submit = submit
        self._checkpoint_offset = scanner.offset if scanner is not None else 0
        self._checkpoint_at = time.monotonic()

    def scan(self, *, final: bool = False) -> tuple[Path, ...]:
        if self._scanner is None:
            return ()
        matches = self._scanner.scan(self._context.log_file, final=final)
        event_files = self._persist_events(matches)
        if matches or self._checkpoint_due(final=final):
            self._write_checkpoint(matches)
        for event_file in event_files:
            self._submit(event_file)
        return event_files

    def _persist_events(self, matches: tuple[StageMatch, ...]) -> tuple[Path, ...]:
        scanner = self._scanner
        assert scanner is not None
        paths: list[Path] = []
        first_sequence = len(scanner.completed) - len(matches) + 1
        for index, match in enumerate(matches):
            paths.append(create_stage_event(
                self._context.log_file,
                sequence=first_sequence + index,
                run_id=self._context.run_id,
                thread_id=self._context.thread_id,
                command=self._context.command,
                stage_id=match.stage_id,
                matched_line=match.line,
                log_offset=match.log_offset,
            ))
        return tuple(paths)

    def _checkpoint_due(self, *, final: bool) -> bool:
        scanner = self._scanner
        assert scanner is not None
        if scanner.offset == self._checkpoint_offset:
            return False
        return (
            final
            or scanner.offset - self._checkpoint_offset >= CHECKPOINT_BYTE_INTERVAL
            or time.monotonic() - self._checkpoint_at >= CHECKPOINT_INTERVAL_SECONDS
        )

    def _write_checkpoint(self, matches: tuple[StageMatch, ...]) -> None:
        scanner = self._scanner
        assert scanner is not None
        if self._context.runtime_file is not None:
            last_event = None
            if matches:
                event_file = stage_event_path(self._context.log_file, len(scanner.completed))
                last_event = {
                    "type": "stage",
                    "stage_id": matches[-1].stage_id,
                    "event_file": str(event_file),
                }
            write_run_runtime(
                self._context.runtime_file,
                run_id=self._context.run_id,
                state="running",
                log_offset=scanner.offset,
                completed_stages=list(scanner.completed),
                last_event=last_event,
            )
        self._checkpoint_offset = scanner.offset
        self._checkpoint_at = time.monotonic()
