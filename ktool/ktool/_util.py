"""Shared helpers for ktool subcommands."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str] | str, *, check: bool = True, env: dict | None = None,
        cwd: Path | str | None = None, capture: bool = False,
        sudo: bool = False, **kw) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if sudo:
        cmd = ["sudo"] + cmd
    merged = {**os.environ, **(env or {})}
    stdout = subprocess.PIPE if capture else None
    return subprocess.run(cmd, check=check, env=merged, cwd=cwd,
                          stdout=stdout, text=capture, **kw)


def die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def require_cmds(*cmds: str) -> None:
    import shutil
    missing = [c for c in cmds if not shutil.which(c)]
    if missing:
        die(f"Required command(s) not found: {', '.join(missing)}")


def timestamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%m%d%Y-%H%M%S")
