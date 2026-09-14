"""Cross-platform command construction for wake-run."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def resolve_powershell() -> str:
    for candidate in ("pwsh", "powershell.exe", "powershell"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise RuntimeError("PowerShell is required to run wake-run commands on Windows.")


def resolve_codex_executable(codex_bin: str, *, platform: str | None = None) -> str:
    platform = platform or os.name
    candidate = Path(codex_bin).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    if platform == "nt" and candidate.parent == Path(".") and not candidate.suffix:
        for suffix in (".exe", ".cmd", ".bat", ".ps1", ""):
            resolved = shutil.which(codex_bin + suffix)
            if resolved:
                return resolved
    else:
        resolved = shutil.which(codex_bin)
        if resolved:
            return resolved
    raise RuntimeError(f"Codex CLI not found: {codex_bin}")


def build_codex_invocation(
    resolved_codex: str,
    args: list[str],
    *,
    platform: str | None = None,
    powershell_bin: str | None = None,
) -> list[str]:
    platform = platform or os.name
    if platform == "nt" and Path(resolved_codex).suffix.lower() == ".ps1":
        shell = powershell_bin or resolve_powershell()
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", resolved_codex, *args]
    return [resolved_codex, *args]


def build_experiment_invocation(
    command: str,
    *,
    platform: str | None = None,
    powershell_bin: str | None = None,
) -> list[str]:
    platform = platform or os.name
    if platform == "nt":
        shell = powershell_bin or resolve_powershell()
        wrapped = (
            f"& {{ {command} }}; $wakeRunOk = $?; $wakeRunExit = $LASTEXITCODE; "
            "if ($null -ne $wakeRunExit) { exit $wakeRunExit }; "
            "if (-not $wakeRunOk) { exit 1 }; exit 0"
        )
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", wrapped]
    bash = shutil.which("bash")
    if bash:
        return [bash, "-o", "pipefail", "-c", command]
    shell = os.environ.get("SHELL") or "/bin/sh"
    if not Path(shell).is_file():
        shell = "/bin/sh"
    return [shell, "-c", command]


def detached_popen_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs
