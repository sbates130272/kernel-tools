"""build-amdgpu-dkms — stage and build the amdgpu DKMS module.

Ports scripts/build-amdgpu-dkms.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from ktool._util import run, die


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("build-amdgpu-dkms",
                        help="Stage and build the amdgpu DKMS module")
    p.add_argument("--kernel-ver", required=True,
                   help="Kernel version to build for (e.g. 6.8.0-60-generic)")
    p.add_argument("--kernel-dir", default="./src",
                   help="Path to kernel git repo (default: ./src)")
    p.add_argument("--amdgpu-remote", default="amdgpu",
                   help="Git remote name for the amdgpu tree (default: amdgpu)")
    p.add_argument("--amdgpu-ref", default="master",
                   help="Git ref to stage from (e.g. roc-7.2.x)")
    p.add_argument("--patch-dirs", default="",
                   help="Colon-separated list of patch directories to apply")
    p.add_argument("--install", action=argparse.BooleanOptionalAction, default=True,
                   help="Install the built .ko files (default: yes)")
    p.add_argument("--mkdeb", action="store_true",
                   help="Produce a kernel-independent source tarball (dkms mktarball)")
    p.add_argument("--force", action="store_true",
                   help="Force re-stage and rebuild even if already done")
    p.add_argument("--build-src", default="",
                   help="Pre-staged source directory (skips git staging)")
    p.set_defaults(func=run_cmd)


def _check_deps(kernel_ver: str) -> None:
    hdr_dir = Path(f"/lib/modules/{kernel_ver}/build")
    if not hdr_dir.exists():
        die(f"Kernel headers not found at {hdr_dir}.\n"
            f"       Run: sudo apt install linux-headers-{kernel_ver}")


def _stage_source(kernel_dir: Path, remote: str, ref: str,
                  patch_dirs: list[Path], force: bool,
                  stage_root: Path) -> tuple[str, Path]:
    """Stage amdgpu source and return (amdgpu_ver, staged_dir)."""
    run(["git", "fetch", remote, ref], cwd=kernel_dir)
    ver_raw = run(
        ["git", "describe", "--tags", "--abbrev=8",
         f"refs/remotes/{remote}/{ref}"],
        cwd=kernel_dir, capture=True, check=False,
    ).stdout.strip()
    if not ver_raw:
        ver_raw = ref.replace("/", "-")

    amdgpu_ver = ver_raw.lstrip("v")
    staged = stage_root / f"amdgpu-{amdgpu_ver}"

    if staged.exists() and not force:
        print(f"Source already staged at {staged}, skipping (use --force to redo)")
        return amdgpu_ver, staged

    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)

    run(["git", "archive", f"{remote}/{ref}"],
        cwd=kernel_dir,
        stdout=subprocess.PIPE, check=False)

    archive = subprocess.Popen(
        ["git", "archive", f"refs/remotes/{remote}/{ref}"],
        cwd=kernel_dir, stdout=subprocess.PIPE,
    )
    subprocess.run(["tar", "-x", "-C", str(staged)],
                   stdin=archive.stdout, check=True)
    archive.wait()

    for pd in patch_dirs:
        for pf in sorted(pd.glob("*.patch")):
            with open(pf, "rb") as fh:
                subprocess.run(["patch", "-p1"], stdin=fh, check=True, cwd=staged)

    return amdgpu_ver, staged


def run_cmd(args: argparse.Namespace) -> int:
    kernel_ver = args.kernel_ver
    _check_deps(kernel_ver)

    patch_dirs = [Path(d) for d in args.patch_dirs.split(":") if d] if args.patch_dirs else []

    stage_root = Path.cwd() / "amdgpu-build"
    stage_root.mkdir(exist_ok=True)

    if args.build_src:
        staged = Path(args.build_src)
        if not staged.exists():
            die(f"--build-src directory not found: {staged}")
        amdgpu_ver = staged.name.removeprefix("amdgpu-")
    else:
        kernel_dir = Path(args.kernel_dir)
        if not (kernel_dir / ".git").exists() and not (kernel_dir / "objects").exists():
            die(f"Kernel git repo not found at {kernel_dir}")
        amdgpu_ver, staged = _stage_source(
            kernel_dir, args.amdgpu_remote, args.amdgpu_ref,
            patch_dirs, args.force, stage_root,
        )

    src_link = Path(f"/usr/src/amdgpu-{amdgpu_ver}")
    if args.force or not src_link.exists():
        run(["ln", "-sfn", str(staged), str(src_link)], sudo=True)

    jobs = os.cpu_count() or 4

    # Inject ccache into dkms.conf if available
    import re, shutil as _sh
    dkms_conf = staged / "amd/dkms/dkms.conf"
    if _sh.which("ccache") and dkms_conf.exists():
        content = dkms_conf.read_text()
        if "ccache" not in content:
            patched = re.sub(
                r"(MAKE\[0\]=\"'make')('.*)",
                r"\1 CC='ccache gcc'\2",
                content,
            )
            dkms_conf.write_text(patched)
            print("ccache injected into dkms.conf")

    dkms_name = f"amdgpu/{amdgpu_ver}"

    if args.force:
        run(["dkms", "remove", dkms_name, "-k", kernel_ver],
            sudo=True, check=False)

    run(["dkms", "add", dkms_name], sudo=True, check=False)
    run(["dkms", "build", dkms_name, "-k", kernel_ver,
         "-j", str(jobs)], sudo=True)

    if args.install:
        run(["dkms", "install", dkms_name, "-k", kernel_ver,
             "--force"], sudo=True)
        print(f"amdgpu {amdgpu_ver} installed for {kernel_ver}")
    else:
        print(f"amdgpu {amdgpu_ver} built (not installed) for {kernel_ver}")

    if args.mkdeb:
        run(["dkms", "mktarball", dkms_name, "--source-only"], sudo=True)
        print("Source tarball created.")

    return 0
