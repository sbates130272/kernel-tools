"""fast-build-amdgpu — incremental amdgpu OOT build without DKMS overhead.

Ports scripts/fast-build-amdgpu and scripts/docker-fast-build-amdgpu.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path

from ktool._util import run, die


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("fast-build-amdgpu",
                        help="Incremental amdgpu OOT build (persistent .o files)")
    p.add_argument("--kernel-ver", required=True,
                   help="Kernel version to build for (e.g. 7.0.0-34-generic)")
    p.add_argument("--kernel-dir", default="./src",
                   help="Path to kernel git repo (default: ./src)")
    p.add_argument("--amdgpu-remote", default="amdgpu",
                   help="Git remote for the amdgpu tree (default: amdgpu)")
    p.add_argument("--amdgpu-ref", default="master",
                   help="Git ref to build (e.g. roc-7.2.x)")
    p.add_argument("--patch-dirs", default="",
                   help="Colon-separated patch directories")
    p.add_argument("--install", action=argparse.BooleanOptionalAction, default=True,
                   help="modprobe the built .ko files (default: yes)")
    p.add_argument("--force", action="store_true",
                   help="Force full re-stage and rebuild")
    p.add_argument("--build-src", default="",
                   help="Pre-staged source directory")
    p.add_argument("--stage-root", default="./amdgpu-build",
                   help="Where to stage amdgpu source (default: ./amdgpu-build)")
    p.add_argument("--build-out-base", default="./amdgpu-build-out",
                   help="Persistent kernel build output dir (default: ./amdgpu-build-out)")
    p.add_argument("--docker", action="store_true",
                   help="Run inside the amdgpu-dkms-build Docker container")
    p.add_argument("--image", default="",
                   help="Docker image to use (default: amdgpu-dkms-build:<kernel-ver>)")
    p.add_argument("--ccache-maxsize", default="10G",
                   help="Max ccache size when using Docker (default: 10G)")
    p.set_defaults(func=run_cmd)


def _source_hash(staged: Path, patch_dirs: list[Path]) -> str:
    import hashlib
    h = hashlib.sha256()
    # Hash staged dir mtime + patch files
    for p in sorted(staged.rglob("*")):
        if p.is_file():
            h.update(str(p.stat().st_mtime_ns).encode())
    for pd in patch_dirs:
        for pf in sorted(pd.glob("*.patch")):
            h.update(pf.read_bytes())
    return h.hexdigest()[:16]


def _stage(kernel_dir: Path, remote: str, ref: str, patch_dirs: list[Path],
           force: bool, stage_root: Path) -> tuple[str, Path]:
    run(["git", "fetch", remote, ref], cwd=kernel_dir)
    ver_raw = run(
        ["git", "describe", "--tags", "--abbrev=8",
         f"refs/remotes/{remote}/{ref}"],
        cwd=kernel_dir, capture=True, check=False,
    ).stdout.strip() or ref.replace("/", "-")

    amdgpu_ver = ver_raw.lstrip("v")
    staged = stage_root / f"amdgpu-{amdgpu_ver}"

    hash_file = stage_root / f".hash-{amdgpu_ver}"
    cur_hash = ""
    if hash_file.exists():
        cur_hash = hash_file.read_text().strip()

    if staged.exists() and not force:
        new_hash = _source_hash(staged, patch_dirs)
        if new_hash == cur_hash:
            print(f"Source unchanged ({amdgpu_ver}), skipping stage")
            return amdgpu_ver, staged

    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)

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

    hash_file.write_text(_source_hash(staged, patch_dirs))
    return amdgpu_ver, staged


def _run_docker(args: argparse.Namespace) -> int:
    kernel_ver = args.kernel_ver
    image = args.image or f"amdgpu-dkms-build:{kernel_ver}"
    repo_root = Path.cwd()
    build_out_base = "/buildout"

    docker_cmd = [
        "docker", "run", "--rm",
        "-v", "amdgpu-dkms-ccache:/root/.ccache",
        "-v", f"amdgpu-dkms-buildout:{build_out_base}",
        "-e", f"KERNEL_VER={kernel_ver}",
        "-e", f"AMDGPU_REF={args.amdgpu_ref}",
        "-e", f"PATCH_DIRS={args.patch_dirs}",
        "-e", f"INSTALL={'yes' if args.install else 'no'}",
        "-e", f"FORCE={'yes' if args.force else 'no'}",
        "-e", f"BUILD_OUT_BASE={build_out_base}",
        "-e", f"CCACHE_MAXSIZE={args.ccache_maxsize}",
    ]
    if args.build_src:
        src = Path(args.build_src).resolve()
        docker_cmd += ["-v", f"{src}:{src}:ro", "-e", f"BUILD_SRC={src}"]
    if args.kernel_dir:
        kd = Path(args.kernel_dir).resolve()
        docker_cmd += ["-v", f"{kd}:{kd}:ro", "-e", f"KERNEL_DIR={kd}"]

    # Bind-mount the host /lib/modules so the container can find the kernel
    # headers at their standard path (/lib/modules/{kver}/build).  This avoids
    # baking headers into the image, which fails when the pinned version is not
    # in the Ubuntu apt repos used by the Dockerfile.
    docker_cmd += ["-v", "/lib/modules:/lib/modules:ro"]
    docker_cmd += ["-v", f"{repo_root}:{repo_root}", "-w", str(repo_root)]
    # Install ktool from the mounted repo then run it. The image runs as root
    # while the mounted repo may be owned by the CI runner user — mark it safe
    # so git doesn't refuse to operate in it. PEP 668 requires --break-system-packages.
    inner_args = [
        f"--kernel-ver {kernel_ver}",
        f"--amdgpu-ref {args.amdgpu_ref}",
        f"--build-out-base {build_out_base}",
    ]
    if args.build_src:
        inner_args.append(f"--build-src {Path(args.build_src).resolve()}")
    if args.patch_dirs:
        inner_args.append(f"--patch-dirs {args.patch_dirs}")
    if not args.install:
        inner_args.append("--no-install")
    if args.force:
        inner_args.append("--force")
    inner_cmd = (
        f"git config --global --add safe.directory '*' && "
        f"python3 -m pip install -q --break-system-packages {repo_root}/ktool && "
        f"ktool fast-build-amdgpu {' '.join(inner_args)}"
    )
    docker_cmd += [image, "bash", "-c", inner_cmd]
    return subprocess.run(docker_cmd).returncode


def run_cmd(args: argparse.Namespace) -> int:
    if args.docker:
        return _run_docker(args)

    kernel_ver = args.kernel_ver
    hdr_dir = Path(f"/lib/modules/{kernel_ver}/build")
    if not hdr_dir.exists():
        die(f"Kernel headers not found at {hdr_dir}")

    patch_dirs = [Path(d) for d in args.patch_dirs.split(":") if d] if args.patch_dirs else []
    stage_root = Path(args.stage_root)
    stage_root.mkdir(exist_ok=True)
    build_out_base = Path(args.build_out_base)

    if args.build_src:
        staged = Path(args.build_src)
        if not staged.exists():
            die(f"--build-src not found: {staged}")
        amdgpu_ver = staged.name.removeprefix("amdgpu-")
    else:
        kernel_dir = Path(args.kernel_dir)
        amdgpu_ver, staged = _stage(
            kernel_dir, args.amdgpu_remote, args.amdgpu_ref,
            patch_dirs, args.force, stage_root,
        )

    build_out = build_out_base / f"amdgpu-{amdgpu_ver}" / kernel_ver
    if args.force:
        shutil.rmtree(build_out, ignore_errors=True)
    build_out.mkdir(parents=True, exist_ok=True)

    # The kernel OOT build (M=) puts object files alongside source — there is
    # no clean source/output separation via make variables.  Rsync the staged
    # source into build_out so incremental rebuilds preserve .o files between
    # runs while the original staged tree stays clean.
    run(["rsync", "-a", "--delete",
         f"{staged}/", f"{build_out}/"])

    # Run the amdgpu configure script (generated by autoconf during staging)
    # against the kernel headers.  This is the equivalent of the DKMS pre_build
    # step that generates compat headers (kcl/) and sets HAVE_* flags required
    # for the build to succeed against the actual running kernel.
    # configure must run from amd/dkms/ so it writes config/config.h at
    # amd/dkms/config/config.h — the path the Kbuild expects.
    configure = build_out / "amd" / "dkms" / "configure"
    if configure.exists():
        run([str(configure),
             f"--with-linux-obj={hdr_dir}",
             f"--with-linux={hdr_dir}"],
            cwd=build_out / "amd" / "dkms")

    # Some amdgpu versions reference include/rename_symbol.h via a -include
    # compiler flag in their Kbuild but don't ship the file.  Create a stub so
    # the compiler flag doesn't cause a fatal error.
    stub = build_out / "include" / "rename_symbol.h"
    stub.parent.mkdir(parents=True, exist_ok=True)
    if not stub.exists():
        stub.touch()

    # DRM_VER / DRM_PATCH / DRM_SUB are normally defined in drm/drm_version.h
    # which is generated at kernel build time and not available in the headers
    # package.  Provide fallback definitions so amdgpu_drv.c can compile.
    drm_version_stub = build_out / "include" / "kcl" / "drm_version_fallback.h"
    if not drm_version_stub.exists():
        drm_version_stub.write_text(
            "#ifndef DRM_VER\n"
            "#define DRM_VER 0\n"
            "#define DRM_PATCH 0\n"
            "#define DRM_SUB 0\n"
            "#endif\n"
        )
    env_cflags = f"-include {drm_version_stub}"

    env: dict[str, str] = {}
    # Force-include the DRM version fallback and suppress warnings that arise
    # from KCL compat headers when the exact kernel version isn't matched.
    env["KCFLAGS"] = (
        f"{env_cflags} "
        "-Wno-error=undef "
        "-Wno-error=implicit-function-declaration "
        "-Wno-error=incompatible-pointer-types"
    )
    if shutil.which("ccache"):
        env["CC"] = "ccache gcc"
        env["CXX"] = "ccache g++"
        env["CCACHE_DIR"] = os.environ.get("CCACHE_DIR",
                                            str(Path.home() / ".ccache"))
        print("ccache: enabled")

    jobs = os.cpu_count() or 4
    run(["make", "-C", str(hdr_dir), f"M={build_out}",
         f"-j{jobs}", "modules"],
        env={**os.environ, **env})

    ko_files = list(build_out.glob("**/*.ko"))
    print(f"Built {len(ko_files)} .ko file(s) in {build_out}")

    if args.install and ko_files:
        for ko in ko_files:
            run(["modprobe", "-v", ko.stem], sudo=True, check=False)

    return 0
