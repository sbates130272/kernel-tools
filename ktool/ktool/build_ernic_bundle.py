"""build-ernic-bundle — build all .deb packages for the ernic+rocjitsu stack.

Produces five deliverables in <dist-dir>/:
  1. linux-*.deb          patched v7.2.15 kernel (DMA-BUF + io_uring P2P)
  2. amdgpu-dkms_*.deb   fetched from AMD ROCm apt repo
  3. ionic-dkms_*.deb    ionic + ionic_rdma + ernic_p4 built against kernel headers
  4. ernic-config_*.deb  modprobe.d / udev / modules-load.d fragments
  5. rdma-core_*.deb     userspace libibverbs provider with ionic SRQ patches

Firmware (ip_discovery.bin) is generated at runtime from the rocjitsu container
and is intentionally NOT produced here.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

from ktool._util import run, die, require_cmds

_ERNIC_REMOTE = "https://github.com/ROCm/rocm-ernic.git"
_KERNEL_REMOTE = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable.git"
_AMDGPU_DKMS_PKG = "amdgpu-dkms"
_ROCM_APT_REPO = "https://repo.radeon.com/rocm/apt/6.3 jammy main"
_ROCM_GPG_URL = "https://repo.radeon.com/rocm/rocm.gpg.key"
_RDMA_CORE_REMOTE = "https://github.com/linux-rdma/rdma-core.git"


def _load_pins(pins_file: Path) -> dict:
    """Load ernic-pins.yml; return empty dict if absent or yaml unavailable."""
    if not pins_file.exists():
        return {}
    try:
        import yaml  # type: ignore
        with open(pins_file) as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        import re
        pins: dict = {}
        for line in pins_file.read_text().splitlines():
            m = re.match(r'^(\w+):\s*(\S+)', line)
            if m:
                pins[m.group(1)] = m.group(2)
        return pins


def _default_pins() -> dict:
    """Locate ernic-pins.yml relative to the repo root."""
    pins_file = _repo_root() / "ernic-pins.yml"
    return _load_pins(pins_file)


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "build-ernic-bundle",
        help="Build all .deb packages for the ernic+rocjitsu+dmabuf stack",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            Builds five .deb packages into <dist-dir>:
              1. Patched v7.2.15 kernel (DMA-BUF + io_uring P2P patchsets)
              2. amdgpu-dkms fetched from AMD ROCm apt repo
              3. ionic DKMS modules (ionic, ionic_rdma, ernic_p4)
              4. ernic-config (modprobe.d, udev, modules-load.d)
              5. rdma-core userspace provider with ionic SRQ patches

            Firmware blobs (ip_discovery.bin) are NOT produced; they must be
            generated at runtime from the rocjitsu container image.
        """),
    )
    p.add_argument("--dist-dir", default="dist/ernic-bundle",
                   help="Output directory for produced .deb files (default: dist/ernic-bundle)")
    p.add_argument("--work-dir", default=".build-ernic-bundle-work",
                   help="Scratch directory for intermediate build artifacts")
    p.add_argument("--threads", type=int, default=max(1, os.cpu_count() or 8),
                   help="Parallel build jobs (default: nproc)")
    pins = _default_pins()
    default_tag = pins.get("kernel_tag", "v7.2.15")
    default_commit = pins.get("ernic_source_pin", "")

    p.add_argument("--kernel-tag", default=default_tag,
                   help=f"Kernel stable tag to build (default: from ernic-pins.yml)")
    p.add_argument("--ernic-commit", default=default_commit,
                   help="rocm-ernic commit SHA (default: from ernic-pins.yml)")
    p.add_argument("--ernic-remote", default=_ERNIC_REMOTE,
                   help="rocm-ernic git remote URL")
    p.add_argument("--rdma-core-sha", default="",
                   help="rdma-core commit SHA to check out (read from ernic repo if empty)")
    p.add_argument("--skip-kernel", action="store_true",
                   help="Skip kernel build (reuse existing debs in dist-dir)")
    p.add_argument("--skip-amdgpu", action="store_true",
                   help="Skip amdgpu-dkms fetch")
    p.add_argument("--skip-ionic", action="store_true",
                   help="Skip ionic DKMS build")
    p.add_argument("--skip-config", action="store_true",
                   help="Skip ernic-config .deb generation")
    p.add_argument("--skip-rdma-core", action="store_true",
                   help="Skip rdma-core .deb build")
    p.add_argument("--patch-dir", default="",
                   help="Override directory containing dmabuf-72x-backport/ and io-uring-dmabuf/ "
                        "patch dirs (default: auto-detect from repo root)")
    p.set_defaults(func=run_cmd)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Return the kernel-tools repo root (parent of the ktool package dir)."""
    here = Path(__file__).parent
    # Installed: site-packages/ktool/ — no patches/ sibling; fall back to cwd
    candidate = here.parent.parent  # ktool/ktool/.. -> ktool/.. -> repo root
    if (candidate / "patches").is_dir():
        return candidate
    return Path.cwd()


def _find_patch_dirs(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.patch_dir:
        base = Path(args.patch_dir)
    else:
        base = _repo_root() / "patches"
    backport = base / "dmabuf-72x-backport"
    iou = base / "io-uring-dmabuf"
    if not backport.is_dir():
        die(f"Patch dir not found: {backport}")
    if not iou.is_dir():
        die(f"Patch dir not found: {iou}")
    return backport, iou


def _apply_patch_dir(patch_dir: Path, src_dir: Path) -> None:
    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        die(f"No .patch files found in {patch_dir}")
    for pf in patches:
        print(f"  Applying {pf.name}")
        with open(pf, "rb") as fh:
            subprocess.run(["patch", "-p1"], stdin=fh, check=True, cwd=src_dir)


def _config_script_path(name: str) -> Path:
    """Locate a config-script by name, checking both installed and repo locations."""
    installed = Path(__file__).parent / "config_scripts" / name
    if installed.exists():
        return installed
    repo = _repo_root() / "config-scripts" / name
    if repo.exists():
        return repo
    die(f"Config script not found: {name}")
    raise SystemExit(1)  # unreachable; satisfies type checker


def _glob_debs(directory: Path, pattern: str) -> list[Path]:
    return sorted(directory.glob(pattern))


def _copy_debs(src_dir: Path, pattern: str, dist: Path) -> list[Path]:
    debs = _glob_debs(src_dir, pattern)
    copied = []
    for d in debs:
        dest = dist / d.name
        shutil.copy2(d, dest)
        copied.append(dest)
    return copied


# ---------------------------------------------------------------------------
# Step 1: kernel
# ---------------------------------------------------------------------------

def _build_kernel(args: argparse.Namespace, work: Path, dist: Path,
                  backport_dir: Path, iou_dir: Path) -> Path:
    """Clone stable kernel, apply patches, build deb-pkg. Returns src dir."""
    src = work / "linux-kernel"
    if not src.exists():
        print(f"[1/5] Cloning kernel {args.kernel_tag} ...")
        run(["git", "clone", "--depth", "1",
             args.kernel_remote, "-b", args.kernel_tag, str(src)])
    else:
        print(f"[1/5] Reusing kernel clone at {src}")

    # Apply patches only if .config does not yet exist (idempotency guard)
    if not (src / ".config").exists():
        print("  Applying dmabuf-72x-backport patches ...")
        _apply_patch_dir(backport_dir, src)
        print("  Applying io-uring-dmabuf patches ...")
        _apply_patch_dir(iou_dir, src)

        # Configure
        script = _config_script_path("config-ernic-bundle")
        run(["make", "defconfig"], cwd=src)
        env = {"SCRIPT_DIR": str(script.parent)}
        run(["bash", str(script)], cwd=src, env={**os.environ, **env})
        run(["make", "olddefconfig"], cwd=src)

    # Build
    print(f"  Building kernel debs (j={args.threads}) ...")
    run(["make", f"-j{args.threads}",
         "LOCALVERSION=-ernic-bundle",
         "deb-pkg"], cwd=src)

    # Collect debs (make deb-pkg emits them one level above src)
    deb_parent = src.parent
    copied = _copy_debs(deb_parent, "linux-*.deb", dist)
    if not copied:
        die("No linux-*.deb files found after kernel build")
    print(f"  Kernel debs: {[d.name for d in copied]}")
    return src


# ---------------------------------------------------------------------------
# Step 2: amdgpu-dkms
# ---------------------------------------------------------------------------

def _fetch_amdgpu_dkms(work: Path, dist: Path) -> None:
    print("[2/5] Fetching amdgpu-dkms from ROCm apt repo ...")
    dl_dir = work / "amdgpu-dkms-download"
    dl_dir.mkdir(parents=True, exist_ok=True)

    # Add ROCm repo key + source if not already present
    key_path = Path("/etc/apt/trusted.gpg.d/rocm.gpg")
    if not key_path.exists():
        run(["bash", "-c",
             f"curl -fsSL {_ROCM_GPG_URL} | sudo gpg --dearmor -o {key_path}"],
            sudo=False)
    src_path = Path("/etc/apt/sources.list.d/rocm.list")
    if not src_path.exists():
        run(["bash", "-c",
             f"echo 'deb {_ROCM_APT_REPO}' | sudo tee {src_path}"],
            sudo=False)
        run(["sudo", "apt-get", "update", "-qq"])

    run(["apt-get", "download", _AMDGPU_DKMS_PKG], cwd=dl_dir, sudo=True)
    copied = _copy_debs(dl_dir, "amdgpu-dkms_*.deb", dist)
    if not copied:
        die("apt-get download did not produce amdgpu-dkms_*.deb")
    print(f"  amdgpu-dkms: {copied[0].name}")


# ---------------------------------------------------------------------------
# Step 3: ionic DKMS
# ---------------------------------------------------------------------------

def _build_ionic_dkms(args: argparse.Namespace, work: Path, dist: Path,
                      kernel_src: Path) -> None:
    print("[3/5] Building ionic DKMS debs ...")
    ernic = work / "rocm-ernic"
    if not ernic.exists():
        run(["git", "clone", args.ernic_remote, str(ernic)])
        run(["git", "checkout", args.ernic_commit], cwd=ernic)
    else:
        print(f"  Reusing rocm-ernic clone at {ernic}")

    # Fetch ionic sources into a staging area
    ionic_src = work / "ionic-sources"
    ionic_src.mkdir(parents=True, exist_ok=True)
    fetch_script = ernic / "ansible" / "files" / "scripts" / "fetch-ionic-sources.sh"
    if not fetch_script.exists():
        die(f"fetch-ionic-sources.sh not found at {fetch_script}")
    run(["bash", str(fetch_script), str(ionic_src)], cwd=ernic)

    # Apply srq patches then p4 patch to the ionic kernel tree
    srq_dir = ernic / "patches" / "srq"
    p4_dir = ernic / "patches" / "p4"
    ionic_kernel_src = ionic_src / "drivers" / "net" / "ethernet" / "pensando" / "ionic"
    if not ionic_kernel_src.is_dir():
        die(f"ionic kernel source not found at {ionic_kernel_src}")

    _apply_patch_dir(srq_dir, ionic_src)
    if p4_dir.is_dir():
        _apply_patch_dir(p4_dir, ionic_src)

    # Determine kernel version string from headers
    kver_file = kernel_src / "include" / "config" / "kernel.release"
    if kver_file.exists():
        kver = kver_file.read_text().strip()
    else:
        result = run(["make", "-s", "kernelrelease"], cwd=kernel_src, capture=True)
        kver = result.stdout.strip()

    headers_dir = kernel_src  # use the build tree directly for DKMS

    # DKMS build (rootless, out-of-tree)
    dkms_tree = work / "dkms-tree"
    dkms_src_tree = work / "dkms-src"
    dkms_tree.mkdir(parents=True, exist_ok=True)
    dkms_src_tree.mkdir(parents=True, exist_ok=True)

    dkms_conf = ionic_src / "dkms.conf"
    if not dkms_conf.exists():
        die(f"dkms.conf not found at {dkms_conf}")

    run(["dkms", "add",
         "--sourcetree", str(dkms_src_tree),
         "--dkmstree", str(dkms_tree),
         str(ionic_src)])
    run(["dkms", "build",
         "--sourcetree", str(dkms_src_tree),
         "--dkmstree", str(dkms_tree),
         "--kernelsourcedir", str(headers_dir),
         "-k", kver])
    run(["dkms", "mkdeb",
         "--sourcetree", str(dkms_src_tree),
         "--dkmstree", str(dkms_tree),
         "-k", kver])

    # ernic_p4 is a separate DKMS module
    p4_src = ernic / "drivers" / "ernic_p4"
    if p4_src.is_dir():
        p4_dkms_tree = work / "dkms-tree-p4"
        p4_dkms_src = work / "dkms-src-p4"
        p4_dkms_tree.mkdir(parents=True, exist_ok=True)
        p4_dkms_src.mkdir(parents=True, exist_ok=True)
        run(["dkms", "add",
             "--sourcetree", str(p4_dkms_src),
             "--dkmstree", str(p4_dkms_tree),
             str(p4_src)])
        run(["dkms", "build",
             "--sourcetree", str(p4_dkms_src),
             "--dkmstree", str(p4_dkms_tree),
             "--kernelsourcedir", str(headers_dir),
             "-k", kver])
        run(["dkms", "mkdeb",
             "--sourcetree", str(p4_dkms_src),
             "--dkmstree", str(p4_dkms_tree),
             "-k", kver])
        _copy_debs(p4_dkms_tree, "**/*.deb", dist)

    copied = _copy_debs(dkms_tree, "**/*.deb", dist)
    print(f"  ionic DKMS debs: {[d.name for d in copied]}")


# ---------------------------------------------------------------------------
# Step 4: ernic-config .deb
# ---------------------------------------------------------------------------

_ERNIC_CONFIG_VERSION = "1.0.0"
_ERNIC_CONFIG_ARCH = "all"


def _build_config_deb(work: Path, dist: Path, ernic_src: Path) -> None:
    print("[4/5] Building ernic-config .deb ...")
    pkg_name = "ernic-config"
    pkg_root = work / "ernic-config-deb"
    if pkg_root.exists():
        shutil.rmtree(pkg_root)

    # Debian control tree
    debian = pkg_root / "DEBIAN"
    debian.mkdir(parents=True)

    udev_dest = pkg_root / "etc" / "udev" / "rules.d"
    udev_dest.mkdir(parents=True)

    modload_dest = pkg_root / "etc" / "modules-load.d"
    modload_dest.mkdir(parents=True)

    modprobe_dest = pkg_root / "etc" / "modprobe.d"
    modprobe_dest.mkdir(parents=True)

    # udev rules — copy from ernic repo if present
    udev_src = ernic_src / "udev" / "99-rocm-ernic.rules"
    if udev_src.exists():
        shutil.copy2(udev_src, udev_dest / "99-rocm-ernic.rules")
    else:
        # Minimal fallback
        (udev_dest / "99-rocm-ernic.rules").write_text(
            '# ROCm ernic udev rules\nSUBSYSTEM=="infiniband_verbs", MODE="0666"\n'
        )

    # modules-load.d
    (modload_dest / "ernic.conf").write_text(
        "ib_core\nib_uverbs\nionic\nionic_rdma\n"
    )

    # modprobe.d — amdgpu discovery=2 required for rocjitsu emulated GPU
    (modprobe_dest / "amdgpu.conf").write_text(
        "options amdgpu discovery=2\n"
    )

    # DEBIAN/control
    (debian / "control").write_text(textwrap.dedent(f"""\
        Package: {pkg_name}
        Version: {_ERNIC_CONFIG_VERSION}
        Architecture: {_ERNIC_CONFIG_ARCH}
        Maintainer: kernel-tools <noreply@amd.com>
        Description: ernic+rocjitsu stack configuration
         Installs modprobe.d, udev rules, and modules-load.d fragments
         for the ionic RDMA, ernic P4, and rocjitsu GPU stack.
    """))

    (debian / "postinst").write_text(textwrap.dedent("""\
        #!/bin/sh
        set -e
        udevadm control --reload-rules || true
        udevadm trigger || true
    """))
    (debian / "postinst").chmod(0o755)

    deb_name = f"{pkg_name}_{_ERNIC_CONFIG_VERSION}_{_ERNIC_CONFIG_ARCH}.deb"
    run(["dpkg-deb", "--build", str(pkg_root), str(dist / deb_name)])
    print(f"  ernic-config: {deb_name}")


# ---------------------------------------------------------------------------
# Step 5: rdma-core
# ---------------------------------------------------------------------------

def _build_rdma_core_deb(args: argparse.Namespace, work: Path, dist: Path,
                         ernic_src: Path) -> None:
    print("[5/5] Building rdma-core .deb ...")

    # Locate rdma-core patches in ernic repo
    rdma_patches_dir = ernic_src / "ansible" / "files" / "patches" / "rdma-core"
    if not rdma_patches_dir.is_dir():
        die(f"rdma-core patches not found at {rdma_patches_dir}")

    # Determine SHA: from args, or from a README in the patches dir
    sha = args.rdma_core_sha
    if not sha:
        readme = rdma_patches_dir / "README.md"
        if readme.exists():
            for line in readme.read_text().splitlines():
                if line.strip().startswith("Base") or "sha" in line.lower() or "commit" in line.lower():
                    parts = line.split()
                    # take the last token that looks like a 40-char hex SHA
                    for tok in reversed(parts):
                        if len(tok) >= 12 and all(c in "0123456789abcdefABCDEF" for c in tok.strip("`")):
                            sha = tok.strip("`")
                            break
                if sha:
                    break
        if not sha:
            sha = "HEAD"
            print("  Warning: could not determine rdma-core SHA from README; using HEAD")

    rdma_src = work / "rdma-core"
    if not rdma_src.exists():
        run(["git", "clone", _RDMA_CORE_REMOTE, str(rdma_src)])
        run(["git", "checkout", sha], cwd=rdma_src)
    else:
        print(f"  Reusing rdma-core clone at {rdma_src}")

    _apply_patch_dir(rdma_patches_dir, rdma_src)

    # Build with cmake
    build_dir = work / "rdma-core-build"
    build_dir.mkdir(parents=True, exist_ok=True)
    run(["cmake",
         "-GNinja",
         f"-DCMAKE_INSTALL_PREFIX=/usr",
         "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
         "-DNO_PYVERBS=1",
         str(rdma_src)], cwd=build_dir)
    run(["ninja", f"-j{args.threads}"], cwd=build_dir)
    run(["cpack", "-G", "DEB"], cwd=build_dir)

    copied = _copy_debs(build_dir, "*.deb", dist)
    if not copied:
        # Fallback: manual fakeroot deb
        _rdma_core_manual_deb(build_dir, dist, rdma_src)
    else:
        print(f"  rdma-core debs: {[d.name for d in copied]}")


def _rdma_core_manual_deb(build_dir: Path, dist: Path, rdma_src: Path) -> None:
    """Fallback: install into a staging root and dpkg-deb it."""
    stage = build_dir / "pkg-stage"
    run(["ninja", "install", f"DESTDIR={stage}"], cwd=build_dir)

    version_file = rdma_src / "CMakeLists.txt"
    version = "unknown"
    for line in version_file.read_text().splitlines():
        if "PACKAGE_VERSION" in line or "VERSION" in line:
            toks = line.split()
            for t in toks:
                if t[0].isdigit() and "." in t:
                    version = t.strip('")')
                    break
        if version != "unknown":
            break

    debian = stage / "DEBIAN"
    debian.mkdir(parents=True, exist_ok=True)
    (debian / "control").write_text(textwrap.dedent(f"""\
        Package: rdma-core-ionic
        Version: {version}
        Architecture: amd64
        Maintainer: kernel-tools <noreply@amd.com>
        Description: rdma-core with ionic SRQ patches
         Userspace libibverbs provider for ionic RDMA (ionic_rdmav*.so).
    """))
    deb_name = f"rdma-core-ionic_{version}_amd64.deb"
    run(["dpkg-deb", "--build", str(stage), str(dist / deb_name)])
    print(f"  rdma-core (manual): {deb_name}")


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_cmd(args: argparse.Namespace) -> int:
    require_cmds("git", "make", "patch", "dpkg-deb")

    dist = Path(args.dist_dir)
    dist.mkdir(parents=True, exist_ok=True)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)

    backport_dir, iou_dir = _find_patch_dirs(args)

    # Ensure ernic repo is cloned (shared by steps 3, 4, 5)
    ernic_src = work / "rocm-ernic"
    if not ernic_src.exists() and not (args.skip_ionic and args.skip_config and args.skip_rdma_core):
        print(f"Cloning rocm-ernic @ {args.ernic_commit[:12]} ...")
        run(["git", "clone", args.ernic_remote, str(ernic_src)])
        run(["git", "checkout", args.ernic_commit], cwd=ernic_src)

    kernel_src: Path | None = None

    if not args.skip_kernel:
        kernel_src = _build_kernel(args, work, dist, backport_dir, iou_dir)
    else:
        print("[1/5] Skipping kernel build.")
        # Attempt to locate existing src dir for header use
        candidate = work / "linux-kernel"
        if candidate.is_dir():
            kernel_src = candidate

    if not args.skip_amdgpu:
        require_cmds("curl", "gpg")
        _fetch_amdgpu_dkms(work, dist)
    else:
        print("[2/5] Skipping amdgpu-dkms fetch.")

    if not args.skip_ionic:
        if kernel_src is None:
            die("Ionic DKMS build requires kernel source; run without --skip-kernel or "
                "point --work-dir at a previous build.")
        require_cmds("dkms")
        _build_ionic_dkms(args, work, dist, kernel_src)
    else:
        print("[3/5] Skipping ionic DKMS build.")

    if not args.skip_config:
        _build_config_deb(work, dist, ernic_src)
    else:
        print("[4/5] Skipping ernic-config .deb.")

    if not args.skip_rdma_core:
        require_cmds("cmake", "ninja")
        _build_rdma_core_deb(args, work, dist, ernic_src)
    else:
        print("[5/5] Skipping rdma-core .deb.")

    # Emit a simple install script
    install_sh = dist / "install.sh"
    install_sh.write_text(textwrap.dedent("""\
        #!/bin/bash
        # Install all ernic-bundle debs in dependency order.
        set -euo pipefail
        DIR="$(cd "$(dirname "$0")" && pwd)"
        sudo dpkg -i "$DIR"/linux-image-*.deb "$DIR"/linux-headers-*.deb 2>/dev/null || true
        sudo dpkg -i "$DIR"/amdgpu-dkms_*.deb 2>/dev/null || true
        sudo dpkg -i "$DIR"/ionic-dkms_*.deb "$DIR"/ernic_p4-dkms_*.deb 2>/dev/null || true
        sudo dpkg -i "$DIR"/ernic-config_*.deb 2>/dev/null || true
        sudo dpkg -i "$DIR"/rdma-core-ionic_*.deb 2>/dev/null || true
        echo "Done. Reboot into the new kernel to activate."
    """))
    install_sh.chmod(0o755)

    print(f"\nAll deliverables in: {dist}/")
    print("  Run install.sh on the target system, then reboot.")
    print("  NOTE: ip_discovery.bin must be generated separately via vfio_guest_firmware.py")
    return 0
