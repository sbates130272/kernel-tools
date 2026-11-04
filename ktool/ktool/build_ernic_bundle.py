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
_AMDGPU_APT_REPO = "https://repo.radeon.com/amdgpu/latest/ubuntu jammy main"
_AMDGPU_GPG_URL = "https://repo.radeon.com/rocm/rocm.gpg.key"
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
                   help="Kernel stable tag to build (default: from ernic-pins.yml)")
    p.add_argument("--kernel-remote", default=_KERNEL_REMOTE,
                   help="Kernel git remote URL")
    p.add_argument("--ernic-commit", default=default_commit,
                   help="rocm-ernic commit SHA (default: from ernic-pins.yml)")
    p.add_argument("--ernic-remote", default=_ERNIC_REMOTE,
                   help="rocm-ernic git remote URL")
    default_ionic_ref = pins.get("ionic_kernel_pin", default_tag)
    p.add_argument("--ionic-kernel-ref", default=default_ionic_ref,
                   help="Kernel ref for ionic sparse-clone (default: from ernic-pins.yml)")
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


def _find_patch_dirs(args: argparse.Namespace) -> Path:
    if args.patch_dir:
        base = Path(args.patch_dir)
    else:
        base = _repo_root() / "patches"
    backport = base / "dmabuf-72x-backport"
    if not backport.is_dir():
        die(f"Patch dir not found: {backport}")
    return backport


def _apply_patch_dir(patch_dir: Path, src_dir: Path) -> None:
    sentinel = src_dir / f".patches-applied-{patch_dir.name}"
    if sentinel.exists():
        print(f"  Patches from {patch_dir.name} already applied, skipping")
        return
    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        die(f"No .patch files found in {patch_dir}")
    for pf in patches:
        print(f"  Applying {pf.name}")
        with open(pf, "rb") as fh:
            subprocess.run(["patch", "-p1"], stdin=fh, check=True, cwd=src_dir)
    sentinel.touch()


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
                  backport_dir: Path) -> Path:
    """Clone stable kernel, apply patches, build deb-pkg. Returns src dir."""
    src = work / "linux-kernel"
    if not src.exists():
        print(f"[1/5] Cloning kernel {args.kernel_tag} ...")
        run(["git", "clone", "--depth", "1",
             args.kernel_remote, "-b", args.kernel_tag, str(src)])
    else:
        print(f"[1/5] Reusing kernel clone at {src}")

    # Apply patches only if .config does not yet exist (idempotency guard).
    # Reset working tree first so a previous partial run (patches applied but
    # build errored before .config was written) doesn't cause conflicts.
    if not (src / ".config").exists():
        run(["git", "reset", "--hard", "HEAD"], cwd=src)
        run(["git", "clean", "-fd"], cwd=src)
        # dmabuf-72x-backport contains all 20 patches (17 upstream + 3 backport
        # fixups). patches/io-uring-dmabuf/ is the upstream mbox archive — not
        # individual .patch files, so it is not applied separately.
        print("  Applying dmabuf-72x-backport patches ...")
        _apply_patch_dir(backport_dir, src)

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
    print("[2/5] Fetching amdgpu-dkms from AMD apt repo ...")
    dl_dir = work / "amdgpu-dkms-download"
    dl_dir.mkdir(parents=True, exist_ok=True)

    # Detect Ubuntu codename at runtime — avoids hardcoding jammy/resolute/etc.
    codename = subprocess.check_output(["lsb_release", "-cs"], text=True).strip()
    base = "https://repo.radeon.com/amdgpu/latest/ubuntu"

    # Fetch the package index and extract the .deb path for amdgpu-dkms
    pkgs_gz = f"{base}/dists/{codename}/main/binary-amd64/Packages.gz"
    raw = subprocess.check_output(["bash", "-c",
                                   f"curl -fsSL {pkgs_gz} | gunzip"],
                                  text=True)
    deb_path: str | None = None
    in_pkg = False
    for line in raw.splitlines():
        if line == f"Package: {_AMDGPU_DKMS_PKG}":
            in_pkg = True
        elif in_pkg and line.startswith("Filename:"):
            deb_path = line.split(None, 1)[1].strip()
            break
        elif in_pkg and line == "":
            break
    if not deb_path:
        die(f"{_AMDGPU_DKMS_PKG} not found in {pkgs_gz}")

    deb_url = f"{base}/{deb_path}"
    deb_name = Path(deb_path).name
    dest = dl_dir / deb_name
    print(f"  Downloading {deb_name} ...")
    run(["curl", "-fsSL", "-o", str(dest), deb_url])
    shutil.copy2(dest, dist / deb_name)
    print(f"  amdgpu-dkms: {deb_name}")


# ---------------------------------------------------------------------------
# Step 3: ionic DKMS
# ---------------------------------------------------------------------------

def _build_ionic_dkms(args: argparse.Namespace, work: Path, dist: Path,
                      kernel_src: Path | None) -> None:
    print("[3/5] Building ionic DKMS debs ...")
    ernic = work / "rocm-ernic"
    if not ernic.exists():
        run(["git", "clone", args.ernic_remote, str(ernic)])
        run(["git", "checkout", args.ernic_commit], cwd=ernic)
    else:
        print(f"  Reusing rocm-ernic clone at {ernic}")

    # Fetch ionic sources into a staging area; the script handles patching
    # internally via git am -- pass absolute paths since it changes directory.
    ionic_src = work / "ionic-sources"
    fetch_script = (ernic / "ansible" / "files" / "scripts" / "fetch-ionic-sources.sh").resolve()
    if not fetch_script.exists():
        die(f"fetch-ionic-sources.sh not found at {fetch_script}")

    srq_dir = (ernic / "ansible" / "files" / "patches" / "srq").resolve()
    p4_dir = (ernic / "ansible" / "files" / "patches" / "p4").resolve()
    if not srq_dir.is_dir():
        die(f"srq patch dir not found: {srq_dir}")

    fetch_cmd = [
        "bash", str(fetch_script),
        "--source-dir", str(ionic_src.resolve()),
        "--kernel-ref", args.ionic_kernel_ref,
        "--patches-dir", str(srq_dir),
    ]
    if p4_dir.is_dir():
        fetch_cmd += ["--patches-dir", str(p4_dir)]
    run(fetch_cmd)

    if not (ionic_src / "drivers" / "net" / "ethernet" / "pensando" / "ionic").is_dir():
        die(f"ionic kernel source not found under {ionic_src}")

    # --- ionic-ernic DKMS source deb ---
    # dkms add/build/mkdeb all require root even with custom trees, so create
    # the deb manually with dpkg-deb; dkms runs on the target via postinst.
    pkg_name = "ionic-ernic"
    pkg_version = args.ionic_kernel_ref.lstrip("v").replace("/", "-")
    deb_root = work / "ionic-ernic-deb-root"
    if deb_root.exists():
        shutil.rmtree(deb_root)

    src_dest = deb_root / "usr" / "src" / f"{pkg_name}-{pkg_version}"
    (src_dest / "eth").mkdir(parents=True)
    (src_dest / "rdma").mkdir(parents=True)
    shutil.copytree(
        str(ionic_src / "drivers" / "net" / "ethernet" / "pensando" / "ionic"),
        str(src_dest / "eth"), dirs_exist_ok=True)
    shutil.copytree(
        str(ionic_src / "drivers" / "infiniband" / "hw" / "ionic"),
        str(src_dest / "rdma"), dirs_exist_ok=True)
    uapi_src = ionic_src / "include" / "uapi" / "rdma"
    if (uapi_src / "ionic-abi.h").exists():
        (src_dest / "uapi" / "rdma").mkdir(parents=True)
        shutil.copy2(str(uapi_src / "ionic-abi.h"), str(src_dest / "uapi" / "rdma"))

    (src_dest / "Kbuild").write_text(textwrap.dedent("""\
        ifneq ($(wildcard $(src)/uapi/rdma/ionic-abi.h),)
        LINUXINCLUDE := -I$(src)/uapi $(LINUXINCLUDE)
        endif
        ccflags-y += -I$(src)/eth
        obj-m += ionic.o
        ionic-y := $(patsubst $(src)/eth/%.c,eth/%.o,$(wildcard $(src)/eth/*.c))
        ccflags-y += -I$(src)/rdma -I$(src)/eth
        obj-m += ionic_rdma.o
        ionic_rdma-y := $(patsubst $(src)/rdma/%.c,rdma/%.o,$(wildcard $(src)/rdma/*.c))
    """))
    (src_dest / "dkms.conf").write_text(textwrap.dedent(f"""\
        PACKAGE_NAME="{pkg_name}"
        PACKAGE_VERSION="{pkg_version}"
        BUILT_MODULE_NAME[0]="ionic"
        DEST_MODULE_LOCATION[0]="/updates/dkms"
        BUILT_MODULE_NAME[1]="ionic_rdma"
        DEST_MODULE_LOCATION[1]="/updates/dkms"
        AUTOINSTALL="no"
        MAKE="make -C ${{kernel_source_dir}} M=${{dkms_tree}}/{pkg_name}/{pkg_version}/build modules"
        CLEAN="make -C ${{kernel_source_dir}} M=${{dkms_tree}}/{pkg_name}/{pkg_version}/build clean"
    """))

    debian = deb_root / "DEBIAN"
    debian.mkdir()
    (debian / "control").write_text(textwrap.dedent(f"""\
        Package: {pkg_name}-dkms
        Version: {pkg_version}
        Architecture: all
        Maintainer: AMD ERNIC <ernic@amd.com>
        Depends: dkms
        Description: ionic + ionic_rdma DKMS source (ernic-patched)
         Patched for AMD ERNIC P4 auxiliary device support. After installing
         the target kernel, build with:
         dkms build -m {pkg_name} -v {pkg_version} -k <kernel-ver>
    """))
    postinst = debian / "postinst"
    postinst.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        set -e
        case "$1" in
            configure)
                dkms add -m {pkg_name} -v {pkg_version} --no-depmod 2>/dev/null || true
                ;;
        esac
    """))
    postinst.chmod(0o755)
    prerm = debian / "prerm"
    prerm.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        set -e
        case "$1" in
            remove|upgrade|deconfigure)
                dkms remove -m {pkg_name} -v {pkg_version} --all --no-depmod 2>/dev/null || true
                ;;
        esac
    """))
    prerm.chmod(0o755)

    ionic_deb = work / f"{pkg_name}-dkms_{pkg_version}_all.deb"
    run(["dpkg-deb", "--build", "--root-owner-group", str(deb_root), str(ionic_deb)])
    shutil.copy2(ionic_deb, dist / ionic_deb.name)
    print(f"  ionic-ernic DKMS deb: {ionic_deb.name}")

    # --- ernic-p4 DKMS source deb ---
    p4_src = ernic / "drivers" / "ernic_p4"
    if not p4_src.is_dir():
        print("  ernic_p4 source not found, skipping")
        return

    p4_dkms_conf = p4_src / "dkms.conf"
    p4_pkg_name = "ernic-p4"
    p4_pkg_version = "0.2.0"
    for line in p4_dkms_conf.read_text().splitlines():
        if line.startswith("PACKAGE_VERSION="):
            p4_pkg_version = line.split("=", 1)[1].strip().strip('"')
            break

    p4_deb_root = work / "ernic-p4-deb-root"
    if p4_deb_root.exists():
        shutil.rmtree(p4_deb_root)

    p4_src_dest = p4_deb_root / "usr" / "src" / f"{p4_pkg_name}-{p4_pkg_version}"
    shutil.copytree(str(p4_src), str(p4_src_dest))

    p4_debian = p4_deb_root / "DEBIAN"
    p4_debian.mkdir()
    (p4_debian / "control").write_text(textwrap.dedent(f"""\
        Package: {p4_pkg_name}-dkms
        Version: {p4_pkg_version}
        Architecture: all
        Maintainer: AMD ERNIC <ernic@amd.com>
        Depends: dkms, {pkg_name}-dkms (= {pkg_version})
        Description: ernic_p4 DKMS source for AMD ERNIC P4 auxiliary device
         Requires ionic sources from {pkg_name}-dkms. After installing the
         target kernel, build with:
         dkms build -m {p4_pkg_name} -v {p4_pkg_version} -k <kernel-ver>
    """))
    p4_postinst = p4_debian / "postinst"
    p4_postinst.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        set -e
        case "$1" in
            configure)
                echo "/usr/src/{pkg_name}-{pkg_version}" > /etc/ernic-p4-ionic-src
                dkms add -m {p4_pkg_name} -v {p4_pkg_version} --no-depmod 2>/dev/null || true
                ;;
        esac
    """))
    p4_postinst.chmod(0o755)
    p4_prerm = p4_debian / "prerm"
    p4_prerm.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        set -e
        case "$1" in
            remove|upgrade|deconfigure)
                dkms remove -m {p4_pkg_name} -v {p4_pkg_version} --all --no-depmod 2>/dev/null || true
                ;;
        esac
    """))
    p4_prerm.chmod(0o755)

    p4_deb = work / f"{p4_pkg_name}-dkms_{p4_pkg_version}_all.deb"
    run(["dpkg-deb", "--build", "--root-owner-group", str(p4_deb_root), str(p4_deb)])
    shutil.copy2(p4_deb, dist / p4_deb.name)
    print(f"  ernic-p4 DKMS deb: {p4_deb.name}")


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
    run(["dpkg-deb", "--build", "--root-owner-group", str(pkg_root), str(dist / deb_name)])
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
         "-DNO_MAN_PAGES=1",
         str(rdma_src.resolve())], cwd=build_dir)
    run(["ninja", f"-j{args.threads}"], cwd=build_dir)
    cpack_ok = subprocess.run(["cpack", "-G", "DEB"], cwd=build_dir).returncode == 0

    copied = _copy_debs(build_dir, "*.deb", dist) if cpack_ok else []
    if not copied:
        _rdma_core_manual_deb(build_dir, dist, rdma_src)
    else:
        print(f"  rdma-core debs: {[d.name for d in copied]}")


def _rdma_core_manual_deb(build_dir: Path, dist: Path, rdma_src: Path) -> None:
    """Fallback: install into a staging root and dpkg-deb it."""
    stage = build_dir / "pkg-stage"
    if stage.exists():
        shutil.rmtree(stage)
    run(["ninja", "install"], cwd=build_dir, env={"DESTDIR": str(stage.resolve())})

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
    run(["dpkg-deb", "--build", "--root-owner-group", str(stage), str(dist / deb_name)])
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

    backport_dir = _find_patch_dirs(args)

    # Ensure ernic repo is cloned (shared by steps 3, 4, 5)
    ernic_src = work / "rocm-ernic"
    if not ernic_src.exists() and not (args.skip_ionic and args.skip_config and args.skip_rdma_core):
        print(f"Cloning rocm-ernic @ {args.ernic_commit[:12]} ...")
        run(["git", "clone", args.ernic_remote, str(ernic_src)])
        run(["git", "checkout", args.ernic_commit], cwd=ernic_src)

    kernel_src: Path | None = None

    if not args.skip_kernel:
        kernel_src = _build_kernel(args, work, dist, backport_dir)
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
        sudo dpkg -i "$DIR"/ionic-ernic-dkms_*.deb "$DIR"/ernic-p4-dkms_*.deb 2>/dev/null || true
        sudo dpkg -i "$DIR"/ernic-config_*.deb 2>/dev/null || true
        sudo dpkg -i "$DIR"/rdma-core-ionic_*.deb 2>/dev/null || true
        echo "Done. Reboot into the new kernel to activate."
    """))
    install_sh.chmod(0o755)

    print(f"\nAll deliverables in: {dist}/")
    print("  Run install.sh on the target system, then reboot.")
    print("  NOTE: ip_discovery.bin must be generated separately via vfio_guest_firmware.py")
    return 0
