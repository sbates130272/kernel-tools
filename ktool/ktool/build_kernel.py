"""build-kernel — build Linux kernel deb/rpm packages from a remote branch.

Ports scripts/build-kernel-debrpm and the three thin wrappers that call it:
  scripts/build-latest-io-uring-dmabuf-kernel
  scripts/build-latest-knod-kernel
  scripts/build-latest-p2pdma-kernel
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from ktool._util import run, die


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("build-kernel",
                        help="Clone a kernel branch and build deb/rpm packages")

    p.add_argument("--remote",
                   default="git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable.git",
                   help="Kernel git remote URL")
    p.add_argument("--branch", default="v4.14.49",
                   help="Branch or tag to build")
    p.add_argument("--config", default="",
                   help="Path to a .config file (default: make defconfig)")
    p.add_argument("--patch", default="",
                   help="Patch file or directory to apply (optional)")
    p.add_argument("--threads", type=int, default=8,
                   help="Parallel build jobs (default: 8)")
    p.add_argument("--arch", default="",
                   help="ARCH for cross-compile (empty = host arch)")
    p.add_argument("--cross-compile", default="",
                   help="CROSS_COMPILE prefix for cross-compile")
    p.add_argument("--mono", action="store_true",
                   help="Build a monolithic (non-modular) kernel")
    p.add_argument("--deb", action="store_true", default=True,
                   help="Build Debian packages (default: true)")
    p.add_argument("--rpm", action="store_true",
                   help="Build RPM packages instead of debs")
    p.add_argument("--work-dir", default=".build-kernel-debrpm-work",
                   help="Scratch directory for the build")

    # Convenience preset: io-uring DMA-BUF kernel
    p.add_argument("--preset-dmabuf", action="store_true",
                   help="Use Anuj's fs-dmabuf-rfc-v11-desc-v2 tree with THREADS=128")
    # Convenience preset: KNOD kernel
    p.add_argument("--preset-knod", action="store_true",
                   help="Build Taehee Yoo's KNOD dev tree (knod-7.2)")
    # Convenience preset: p2pdma stable kernel
    p.add_argument("--preset-p2pdma", action="store_true",
                   help="Build stable p2pdma kernel with p2pdma patches")

    p.set_defaults(func=run_cmd)


def _apply_patch(patch: str, work_dir: Path) -> None:
    patch_path = Path(patch)
    if patch_path.is_dir():
        patches = sorted(patch_path.glob("*.patch"))
        for pf in patches:
            with open(pf, "rb") as fh:
                subprocess.run(["patch", "-p1"], stdin=fh, check=True, cwd=work_dir)
    elif patch_path.is_file():
        with open(patch_path, "rb") as fh:
            subprocess.run(["patch", "-p1"], stdin=fh, check=True, cwd=work_dir)
    else:
        die(f"Patch not found: {patch}")


def run_cmd(args: argparse.Namespace) -> int:
    # Apply presets
    if args.preset_dmabuf:
        args.remote = "https://github.com/anuj7781/linux-kernel"
        args.branch = "anuj/fs-dmabuf-rfc-v11-desc-v2"
        args.threads = 128
    elif args.preset_knod:
        args.remote = "https://github.com/TaeheeYoo/knod"
        args.branch = "knod-7.2"
        args.threads = 128
    elif args.preset_p2pdma:
        args.remote = "git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable.git"
        args.branch = "v5.8.14"
        args.patch = "patches/p2pdma"
        args.threads = 128

    work_dir = Path(args.work_dir)
    src_dir = work_dir / "linux"

    if work_dir.exists() and src_dir.exists():
        print(f"Work dir exists ({work_dir}), reusing clone.")
    else:
        work_dir.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth", "1",
             args.remote, "-b", args.branch, str(src_dir)])

    if args.patch:
        _apply_patch(args.patch, src_dir)

    env: dict[str, str] = {}
    if args.arch:
        env["ARCH"] = args.arch
    if args.cross_compile:
        env["CROSS_COMPILE"] = args.cross_compile

    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            die(f"Config file not found: {args.config}")
        shutil.copy(config_path, src_dir / ".config")
        run(["make", "olddefconfig"], cwd=src_dir, env={**os.environ, **env})
    else:
        run(["make", "defconfig"], cwd=src_dir, env={**os.environ, **env})

    if args.mono:
        run(["scripts/config", "--disable", "MODULES"], cwd=src_dir)
        run(["make", "olddefconfig"], cwd=src_dir, env={**os.environ, **env})

    if args.rpm:
        run(["make", f"-j{args.threads}", "rpm-pkg"],
            cwd=src_dir, env={**os.environ, **env})
    else:
        run(["make", f"-j{args.threads}", "deb-pkg"],
            cwd=src_dir, env={**os.environ, **env})

    print(f"Build complete. Packages in {work_dir}/")
    return 0
