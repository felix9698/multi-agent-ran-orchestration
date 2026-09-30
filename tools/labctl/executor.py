"""Bounded local and SSH command execution with sanitized evidence."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from .models import CommandSpec, HostSpec


_SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
_LABEL = re.compile(r"^[A-Za-z0-9_.-]+$")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|authorization)\s*[:=]\s*(?:Bearer\s+)?[^\s]+"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _redact(value: object) -> str:
    text = _PRIVATE_KEY_BLOCK.sub("[REDACTED]", _text(value))
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


@dataclass(frozen=True)
class CommandResult:
    argv: Tuple[str, ...]
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    started_at: str
    ended_at: str
    dry_run: bool


class CommandExecutor:
    """Execute an already validated command without invoking a local shell."""

    def _effective_argv(self, host: HostSpec, command: CommandSpec) -> Tuple[str, ...]:
        if host.transport == "local":
            return command.argv
        if host.transport == "ssh" and host.target:
            return (
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "--",
                host.target,
                shlex.join(command.argv),
            )
        raise ValueError(f"unsupported host transport: {host.transport}")

    def run(
        self,
        host: HostSpec,
        command: CommandSpec,
        *,
        run_dir: Path,
        label: str,
        dry_run: bool,
    ) -> CommandResult:
        if not _LABEL.fullmatch(label):
            raise ValueError("label contains unsafe characters")
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(run_dir, 0o700)
        stdout_path = run_dir / f"{label}.stdout"
        stderr_path = run_dir / f"{label}.stderr"
        effective = self._effective_argv(host, command)
        started_at = _utc_now()
        exit_code = 0
        stdout = "DRY-RUN: " + shlex.join(effective) + "\n" if dry_run else ""
        stderr = ""
        if not dry_run:
            environment = {
                key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ
            }
            try:
                completed = subprocess.run(
                    effective,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=command.timeout_seconds,
                    env=environment,
                    shell=False,
                )
                exit_code = completed.returncode
                stdout = _text(completed.stdout)
                stderr = _text(completed.stderr)
            except subprocess.TimeoutExpired as exc:
                exit_code = 124
                stdout = _text(exc.output)
                stderr = f"TIMEOUT after {command.timeout_seconds}s\n{_text(exc.stderr)}"
        ended_at = _utc_now()
        _write_private(stdout_path, _redact(stdout))
        _write_private(stderr_path, _redact(stderr))
        return CommandResult(
            argv=command.argv,
            exit_code=exit_code,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=started_at,
            ended_at=ended_at,
            dry_run=dry_run,
        )
