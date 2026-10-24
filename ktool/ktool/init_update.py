"""init-update — initialise or update the kernel source git repo.

Ports scripts/init-update.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ktool._util import run, die

_REMOTES: dict[str, str] = {
    "linus":        "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
    "stable":       "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
    "wsl2":         "https://github.com/microsoft/WSL2-Linux-Kernel.git",
    "lk-library":  "https://github.com/lkl/linux.git",
    "amdgpu":       "https://github.com/ROCm/amdgpu.git",
    "ubuntu-noble": "git://git.launchpad.net/~ubuntu-kernel/ubuntu/+source/linux/+git/noble",
}


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("init-update",
                        help="Initialise or update the kernel source git repo")
    p.add_argument("--kernel-dir", default="./src",
                   help="Kernel source directory (default: ./src)")
    p.add_argument("--depth", type=int, default=1,
                   help="Clone depth (default: 1, use 0 for full history)")
    p.add_argument("--check-out", action="store_true",
                   help="Check out the latest stable tag after fetch")
    p.add_argument("--skip-launchpad", action="store_true",
                   help="Skip adding the Ubuntu Launchpad remote (slow network)")
    p.add_argument("--add-remote", nargs=2, metavar=("NAME", "URL"), action="append",
                   default=[], help="Add an extra remote (repeatable)")
    p.set_defaults(func=run_cmd)


def run_cmd(args: argparse.Namespace) -> int:
    kernel_dir = Path(args.kernel_dir).resolve()
    remotes = dict(_REMOTES)

    if args.skip_launchpad:
        remotes.pop("ubuntu-noble", None)

    for name, url in args.add_remote:
        remotes[name] = url

    if not kernel_dir.exists():
        kernel_dir.mkdir(parents=True)
        run(["git", "init"], cwd=kernel_dir)

    # Add missing remotes.
    existing = run(["git", "remote", "-v"], cwd=kernel_dir,
                   capture=True).stdout.lower()
    for name, url in remotes.items():
        if name.lower() not in existing:
            print(f"Adding remote {name}: {url}")
            run(["git", "remote", "add", name, url], cwd=kernel_dir)
        else:
            print(f"Remote already present: {name}")

    # Fetch all remotes.
    depth_args = [f"--depth={args.depth}"] if args.depth > 0 else []
    for name in remotes:
        print(f"Fetching {name}...")
        run(["git", "fetch", name, *depth_args], cwd=kernel_dir, check=False)

    if args.check_out:
        # Find and check out the latest stable tag.
        tags = run(["git", "tag", "-l", "v[0-9]*", "--sort=-version:refname"],
                   cwd=kernel_dir, capture=True).stdout.strip().splitlines()
        if tags:
            latest = tags[0]
            print(f"Checking out {latest}")
            run(["git", "checkout", latest], cwd=kernel_dir)
        else:
            print("No stable tags found; skipping checkout.")

    print(f"Kernel source ready at {kernel_dir}")
    return 0
