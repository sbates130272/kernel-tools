"""man — open the ktool man page."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("man", help="open the ktool man page")
    p.set_defaults(func=run_cmd)


def run_cmd(args: argparse.Namespace) -> int:
    man_page = Path(__file__).parent / "man" / "ktool.1"
    if not man_page.exists():
        print(f"man page not found at {man_page}", file=sys.stderr)
        return 1
    try:
        subprocess.run(["man", str(man_page)], check=True)
    except FileNotFoundError:
        print("man(1) not found; falling back to less", file=sys.stderr)
        pager = os.environ.get("PAGER", "less")
        subprocess.run(
            f"nroff -man {man_page} | {pager}",
            shell=True,
            check=False,
        )
    return 0
