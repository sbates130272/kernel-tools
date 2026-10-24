#!/usr/bin/env python3
"""DMA-BUF P2P test: rocjitsu GPU + NVMe VM, 7.2.x backport kernel.

Optimised Python port of run-this.sh.  Key improvements over the shell script:
  - Parallel: amdgpu source copy from VM overlaps kernel build
  - ccache wraps the kernel build (CC/CXX) for fast incremental rebuilds
  - DKMS build skipped when built .ko files already exist for KVER
  - Kernel install + reboot skipped when VM already runs the target kernel
  - amdgpu source scp skipped when /tmp/amdgpu-7.1.9-src already exists
  - Per-stage wall-clock timing printed at end (cold and warm path annotated)

Prerequisites: oras, qemu-tool, docker, kernel build tools,
               ccache (optional but recommended)
"""

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_DIR    = Path(__file__).resolve().parent
QCOW2_IMAGE = (
    "docker.io/sbates130272/"
    "batesste-ci-images-ubuntu-qcow2-gen-ernic-rocjitsu:"
    "20261022.g1177df0-vm.resolute-ernic-rocjitsu-qm.c079195-qcow2"
)
ROCJITSU_IMAGE = (
    "docker.io/sbates130272/"
    "batesste-ci-images-ubuntu-rocm-rocjitsu:"
    "20261022.g1177df0-rocjitsu.98f6a72"
)
QEMU_IMAGE = (
    "docker.io/sbates130272/"
    "batesste-ci-images-ubuntu-qemu-libvfio-user-sbates-fork:"
    "20261022.g1177df0-qemu.f91f120-vfu.3719c3a"
)
VM_IMAGES_DIR   = Path(os.environ.get(
    "VM_IMAGES_DIR",
    "/var/lib/qemu-tool/images"
    if Path("/var/lib/qemu-tool").exists() or os.getuid() == 0
    else str(Path.home() / ".local/share/qemu-tool/images")
))
VM_SSH_PORT     = int(os.environ.get("VM_SSH_PORT", "2232"))
VM_SSH_HOST     = os.environ.get("VM_SSH_HOST", "localhost")
VM_NAME         = "dmabuf-test"
VM_VCPUS        = int(os.environ.get("VM_VCPUS", "4"))
VM_MEM      = int(os.environ.get("VM_MEM", "4096"))
KERNEL_LABEL    = "-dmabuf-72x"
KERNEL_BRANCH   = os.environ.get("KERNEL_BRANCH", "v7.2.4")
KERNEL_SRC      = Path(os.environ.get("KERNEL_SRC", "/tmp/linux-dmabuf"))
AMDGPU_VER      = "7.1.9-2407772.26.04"
AMDGPU_SRC_VM   = f"/usr/src/amdgpu-{AMDGPU_VER}"
AMDGPU_SRC_HOST = Path(f"/tmp/amdgpu-{AMDGPU_VER.split('-')[0]}-src")

# Verbatim from run-this.sh — do not split or reformat.
IP_DISCOVERY_B64 = (
    "BxQhKAEAAADEKlADPACrIzwCAAB4An8BUAAAAMgCMQGIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAASVBEUwQAPAIAAAAAAQAAAIwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAkACwAABAwBAABgEgAAAAAAAACgAAAAAAAAAMABAAAAAAAALEACAAAAAP8AAA8PAAgAAGABAAAAAAAAYgEAAAAAAADOAQAAAAAAAADcAAAAAAAAAOAAAAAAAAAA5AAAAAAAAADoAAAAAAAAAOwAAAAAAAAA8AAAAAAAAARAAgAAAAAA/EMCAAAAAADURAIAAAAAAAAgAwAAAAAAACQDAAAAAAAAKAMAAAAAAQAADw8ACAAAYAEAAAAAAABiAQAAAAAAAM4BAAAAAAAAANwAAAAAAAAA4AAAAAAAAADkAAAAAAAAAOgAAAAAAAAA7AAAAAAAAADwAAAAAAAABEACAAAAAAD8QwIAAAAAANREAgAAAAAAACADAAAAAAAAJAMAAAAAAAAoAwAAAAAoAAACBwEAAKAQAAAAAAAAAKBAAgAAAABsAAAGBwsAAAAAAAAAAAAAFAAAAAAAAAAgDQAAAAAAAAAEAQAAAAAAALBBAgAAAAAAAAQEAAAAACkAAAIHAAAAIA8AAAAAAAAApEACAAAAACIAAAIEAQAAAKABAAAAAAAAiEACAAAAACMAAAIEAQAAAAwAAAAAAAAAjEACAAAAACoAAAQHAQAAYBIAAAAAAAAAoAAAAAAAAADAAQAAAAAAACxAAgAAAABHQwAAAgAAAFAAAAACAAAACAAAAAIAAAACAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAAAQAAAAIAAAAEABAAACAAAAAQAAAEhBUlYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
)

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

_timings: list[tuple[str, float, str]] = []


class _Timer:
    def __init__(self, label: str):
        self.label = label
        self._start: float = 0.0
        self.cold: bool = True

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *_):
        elapsed = time.monotonic() - self._start
        _timings.append((self.label, elapsed, "cold" if self.cold else "warm"))


def print_timing_summary():
    print("\n" + "=" * 60)
    print("Timing summary")
    print("=" * 60)
    total = 0.0
    for label, elapsed, kind in _timings:
        tag = f"[{kind}]"
        print(f"  {tag:8s} {label:<42s} {elapsed:7.1f}s")
        total += elapsed
    print("-" * 60)
    print(f"  {'':8s} {'TOTAL':<42s} {total:7.1f}s")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str] | str, *, check=True, env=None, cwd=None,
        capture=False, sudo=False, **kw) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if sudo and os.getuid() != 0 and shutil.which("sudo"):
        cmd = ["sudo"] + cmd
    merged_env = {**os.environ, **(env or {})}
    stdout = subprocess.PIPE if capture else None
    return subprocess.run(cmd, check=check, env=merged_env, cwd=cwd,
                          stdout=stdout, text=capture, **kw)


def _vm_ssh_args(vm_user: str) -> list[str]:
    return [
        "ssh",
        "-i", str(VM_IMAGES_DIR / "id_rsa"),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "-p", str(VM_SSH_PORT),
        f"{vm_user}@{VM_SSH_HOST}",
    ]


def _vm_scp_args() -> list[str]:
    return [
        "scp",
        "-i", str(VM_IMAGES_DIR / "id_rsa"),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "-P", str(VM_SSH_PORT),
    ]


def vm_is_up() -> bool:
    # Try qemu-tool inspect first (v1.5+); fall back to docker container check.
    r = run(["qemu-tool", "inspect", "--vm-name", VM_NAME],
            check=False, capture=True, stderr=subprocess.DEVNULL)
    if r.returncode == 0:
        return True
    # inspect subcommand absent in older qemu-tool — check for the QEMU container.
    r2 = run(["docker", "inspect", "--format", "{{.State.Running}}",
              f"vfio-user-rocjitsu-vm-qemu-1"],
             check=False, capture=True, stderr=subprocess.DEVNULL)
    return r2.returncode == 0 and r2.stdout.strip() == "true"


def wait_for_ssh(ssh_args: list[str], *, timeout=300, interval=3):
    start = time.monotonic()
    deadline = start + timeout
    attempt = 0
    while time.monotonic() < deadline:
        r = run(ssh_args + ["true"], check=False, capture=True,
                stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            print(f"\rSSH ready after {time.monotonic() - start:.0f}s          ")
            return
        attempt += 1
        elapsed = time.monotonic() - start
        print(f"\rWaiting for SSH... {elapsed:.0f}s (attempt {attempt})",
              end="", flush=True)
        time.sleep(interval)
    print()
    raise TimeoutError("VM SSH did not become ready in time")


def wait_for_socket(container: str, sock_path: str, *, retries=30):
    for _ in range(retries):
        r = run(["docker", "exec", container, "test", "-S", sock_path],
                check=False, capture=True)
        if r.returncode == 0:
            return
        time.sleep(1)


def compose_env() -> dict:
    return {
        "VM_NAME": VM_NAME,
        "VM_IMAGES_DIR": str(VM_IMAGES_DIR),
        "VM_SSH_PORT": str(VM_SSH_PORT),
        "VM_NVME": "1",
        "VM_VCPUS": str(VM_VCPUS),
        "VM_MEM": str(VM_MEM),
        "ROCJITSU_IMAGE": ROCJITSU_IMAGE,
        "QEMU_IMAGE": QEMU_IMAGE,
    }


# ---------------------------------------------------------------------------
# Stage 1: SSH key + VM overlay
# ---------------------------------------------------------------------------

def stage_setup_vm() -> str:
    """Tear down any running VM, fetch OCI metadata if needed, create overlay.
    Returns vm_user."""

    with _Timer("teardown existing VM") as t:
        if vm_is_up():
            print("VM already up, tearing down...")
        else:
            # Attempt down anyway — a leftover container may still hold port 2222
            # without being detected by qemu-tool inspect.
            t.cold = False
        run(["qemu-tool", "compose", "--stack", "vfio-user-rocjitsu-vm", "down"],
            env=compose_env(), check=False)

    backing = VM_IMAGES_DIR / f"{VM_NAME}-backing.qcow2"
    vm_info = VM_IMAGES_DIR / "vm-info.json"
    id_rsa  = VM_IMAGES_DIR / "id_rsa"

    with _Timer("fetch OCI metadata") as t:
        if vm_info.exists() and id_rsa.exists() and backing.exists():
            print(f"Using cached VM image and metadata: {backing}")
            t.cold = False
        else:
            refs = json.loads(
                run(["oras", "discover", "--format", "json", QCOW2_IMAGE],
                    capture=True).stdout
            )
            manifests = refs.get("manifests", refs.get("referrers", []))
            ref = next(
                r["digest"] for r in manifests
                if r.get("artifactType") == "application/vnd.batesste.vm-info.v1"
            )
            registry = QCOW2_IMAGE.split(":")[0]
            run(["oras", "pull", "--output", str(VM_IMAGES_DIR),
                 f"{registry}@{ref}"])

    with _Timer("gen-vm overlay"):
        run(["qemu-tool", "gen-vm",
             "--vm-name", VM_NAME,
             "--images", str(VM_IMAGES_DIR),
             "--backing-image", QCOW2_IMAGE,
             "--no-compact"])

    id_rsa.chmod(0o600)
    vm_user = json.loads(vm_info.read_text()).get("username", "ubuntu")
    return vm_user


# ---------------------------------------------------------------------------
# Stage 2: Boot
# ---------------------------------------------------------------------------

def stage_boot(vm_user: str) -> tuple[list[str], list[str]]:
    ssh = _vm_ssh_args(vm_user)
    scp = _vm_scp_args()

    with _Timer("compose up + VM ready"):
        run(["qemu-tool", "compose", "--stack", "vfio-user-rocjitsu-vm",
             "up", "--detach"], env=compose_env())
        wait_for_socket("vfio-user-rocjitsu-vm-rocjitsu-1",
                        "/run/vfu/rocjitsu-1.sock")
        wait_for_ssh(ssh)

    kver = run(ssh + ["uname -r"], capture=True).stdout.strip()
    print(f"VM ready (stock kernel): {kver}")
    return ssh, scp


# ---------------------------------------------------------------------------
# Stage 3: Build kernel (with ccache)
# ---------------------------------------------------------------------------

def stage_build_kernel(ssh: list[str], scp: list[str]) -> str:
    """Return KVER. Skips build if cached debs exist in /tmp."""
    deb_pattern = f"/tmp/linux-image-*{KERNEL_LABEL}*.deb"
    cached = sorted(f for f in glob.glob(deb_pattern) if "-dbg_" not in f)
    if cached:
        kver = re.sub(r".*/linux-image-(.*?)_[^_]*_amd64\.deb", r"\1", cached[0])
        print(f"Using cached kernel debs: KVER={kver}")
        with _Timer("kernel build (warm — skipped)") as t:
            t.cold = False
        return kver

    with _Timer("install kernel build deps"):
        run(["apt-get", "install", "-y",
             "bc", "bison", "build-essential", "ccache", "debhelper", "flex",
             "libdw-dev", "libelf-dev", "libncurses-dev", "libssl-dev",
             "nasm", "pahole"], sudo=True)

    with _Timer("git clone kernel") as t:
        if KERNEL_SRC.exists():
            t.cold = False
        else:
            run(["git", "clone", "--depth", "1",
                 "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
                 "-b", KERNEL_BRANCH, KERNEL_SRC.name],
                cwd=KERNEL_SRC.parent)

    patches = sorted((REPO_DIR / "patches/dmabuf-72x-backport").glob("*.patch"))
    base = run(["git", "rev-list", "--max-parents=0", "HEAD"],
               cwd=KERNEL_SRC, capture=True).stdout.strip()
    applied = int(run(["git", "rev-list", "--count", f"{base}..HEAD"],
                      cwd=KERNEL_SRC, capture=True).stdout.strip())

    # Always clean the working tree first — a previous failed git am --3way
    # can leave conflict markers in tracked files even when all patches are
    # committed (warm path). reset --hard HEAD fixes tracked files; clean -fd
    # removes any untracked leftovers.
    run(["git", "am", "--abort"], cwd=KERNEL_SRC, check=False)
    run(["git", "reset", "--hard", "HEAD"], cwd=KERNEL_SRC)
    run(["git", "clean", "-fd"], cwd=KERNEL_SRC)

    with _Timer("apply patches") as t:
        if applied == len(patches):
            print(f"Patches already applied ({len(patches)}), skipping")
            t.cold = False
        else:
            # Reset to base tag and re-apply all patches cleanly.
            run(["git", "reset", "--hard", base], cwd=KERNEL_SRC)
            run(["git", "clean", "-fd"], cwd=KERNEL_SRC)
            run(["git", "am", *[str(p) for p in patches]], cwd=KERNEL_SRC)
            # Tree changed: any existing .orig.tar.gz is now stale.
            for stale in glob.glob(
                    str(KERNEL_SRC.parent / "linux-upstream_*.orig.tar.gz")):
                os.unlink(stale)

    config_file = KERNEL_SRC / ".config"
    with _Timer("configure kernel (build-remote style)") as t:
        if config_file.exists() and applied == len(patches):
            print("Kernel .config already present, skipping configure")
            t.cold = False
        else:
            # Replicate build-remote's setup() using our VM SSH connection:
            # 1. Pull the VM's running kernel config as the base.
            # 2. Get the VM's lsmod for localmodconfig.
            # 3. Apply localmodconfig to trim to what the VM actually needs.
            # 4. Layer config-amdgpu-dkms on top.
            # This ensures drivers like NVMe and virtio survive the config pass.
            vm_kver = run(ssh + ["uname -r"], capture=True).stdout.strip()
            print(f"Pulling VM config for {vm_kver}...")
            run([*scp, f"{ssh[-1]}:/boot/config-{vm_kver}",
                 str(config_file)])
            lsmod_file = KERNEL_SRC / "vm-lsmod"
            lsmod_file.write_text(
                run(ssh + ["lsmod"], capture=True).stdout)
            for flag in ("SYSTEM_TRUSTED_KEYRING", "SYSTEM_TRUSTED_KEYS",
                         "SYSTEM_REVOCATION_KEYS"):
                run(["scripts/config", "--disable", flag], cwd=KERNEL_SRC)
            run(["make", "olddefconfig"], cwd=KERNEL_SRC)
            run(["scripts/config", "--disable", "GPIO_BT8XX",
                 "--module", "BLK_DEV_NULL_BLK"], cwd=KERNEL_SRC)
            run(["make", f"LSMOD={lsmod_file}", "localmodconfig"],
                cwd=KERNEL_SRC)
            run(["scripts/config",
                 "--enable", "LOCALVERSION_AUTO",
                 "--set-str", "LOCALVERSION", KERNEL_LABEL,
                 "--enable", "DEBUG_INFO_NONE",
                 "--disable", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT",
                 "--disable", "DEBUG_INFO_DWARF4",
                 "--disable", "DEBUG_INFO_DWARF5"], cwd=KERNEL_SRC)
            config_script = REPO_DIR / "config-scripts/config-amdgpu-dkms"
            run(["bash", "-c", f"source {config_script}"], cwd=KERNEL_SRC)
            run(["make", "olddefconfig"], cwd=KERNEL_SRC)

    ccache_env: dict[str, str] = {}
    if shutil.which("ccache"):
        ccache_env = {
            "CC": "ccache gcc", "CXX": "ccache g++",
            "CCACHE_DIR": os.environ.get("CCACHE_DIR",
                                         str(Path.home() / ".ccache")),
        }
        print("ccache: enabled")

    with _Timer("make deb-pkg"):
        run(["make", f"-j{os.cpu_count()}", "deb-pkg"],
            cwd=KERNEL_SRC, env=ccache_env or None)

    return run(["make", "-s", "kernelrelease"], cwd=KERNEL_SRC,
               capture=True).stdout.strip()


# ---------------------------------------------------------------------------
# Stage 4: Build amdgpu-dkms on host
# ---------------------------------------------------------------------------

def _copy_amdgpu_src(vm_user: str, scp: list[str]) -> bool:
    """SCP amdgpu source from VM. Returns True on success or already present.

    Tries the known path first. If absent, installs amdgpu-dkms in the VM
    and discovers the actual source path dynamically (slim VMs ship a
    different version string or no DKMS source at all).
    """
    if AMDGPU_SRC_HOST.exists():
        print(f"amdgpu source already at {AMDGPU_SRC_HOST}, skipping scp")
        return True

    # Check if amdgpu-dkms is installed on the runner host (CI pre-install step).
    host_srcs = sorted(Path("/usr/src").glob("amdgpu-*/amd/dkms"))
    if host_srcs:
        host_src = host_srcs[-1].parent  # newest version
        print(f"Using amdgpu source from host: {host_src}")
        shutil.copytree(host_src, AMDGPU_SRC_HOST)
        return True

    ssh = _vm_ssh_args(vm_user)

    def _find_and_copy(remote_path: str) -> bool:
        r = run([*scp, "-r",
                 f"{vm_user}@{VM_SSH_HOST}:{remote_path}",
                 str(AMDGPU_SRC_HOST)],
                check=False, capture=True, stderr=subprocess.DEVNULL)
        return r.returncode == 0

    # Try the expected path first.
    if _find_and_copy(AMDGPU_SRC_VM):
        return True

    # Install amdgpu-dkms if not present, then discover actual source path.
    print("amdgpu source not at expected path, installing amdgpu-dkms in VM...")
    r_apt = run(ssh + [
                    "apt-get",
                    "-o", "Acquire::https::Verify-Peer=false",
                    "-o", "Acquire::https::Verify-Host=false",
                    "install", "-y", "--allow-unauthenticated", "amdgpu-dkms"],
                check=False, capture=True, sudo=True)
    if r_apt.returncode != 0:
        print(f"apt-get failed (exit {r_apt.returncode}):\n{r_apt.stdout[-500:]}")

    # Find where the source landed — version string may differ from AMDGPU_VER.
    r = run(ssh + ["ls -d /usr/src/amdgpu-*/"],
            check=False, capture=True, stderr=subprocess.DEVNULL)
    if r.returncode == 0 and r.stdout.strip():
        remote_path = r.stdout.strip().split()[0].rstrip("/")
        print(f"Found amdgpu source at {remote_path}")
        return _find_and_copy(remote_path)

    # Last resort: use amdgpu source from this repo (amdgpu-build/).
    local_src = next(
        (p for p in sorted((REPO_DIR / "amdgpu-build").glob("amdgpu-roc-*"))
         if (p / "amd" / "dkms").exists()),
        None,
    )
    if local_src:
        print(f"Using repo amdgpu source: {local_src}")
        import shutil as _shutil
        _shutil.copytree(local_src, AMDGPU_SRC_HOST)
        return True

    print("WARNING: no amdgpu source found anywhere")
    return False


def stage_build_dkms(kver: str, vm_user: str, scp: list[str],
                     amdgpu_future=None) -> str | None:
    """Build amdgpu-dkms for KVER without root. Returns MODULES_DIR or None.

    Uses --dkmstree and --sourcetree to keep all DKMS state in user-owned
    temp directories. Kernel headers are extracted (not installed) via
    dpkg-deb --extract so /usr/src is never touched.
    """
    # All DKMS state goes into a user-owned tree under /tmp.
    dkms_tree = Path("/tmp/dkms-build")
    headers_dir = Path(f"/tmp/headers-{kver}")

    # Check for cached .ko files first — skip everything on a warm path.
    # modules_dir is determined after reading dkms.conf (pkg_ver may differ
    # from AMDGPU_VER), so we glob for any matching build under dkms_tree.
    cached_kos = list(dkms_tree.glob(f"amdgpu/*/{kver}/x86_64/module/*.ko*"))
    if cached_kos:
        modules_dir = cached_kos[0].parent
        print(f"DKMS modules already built for {kver}, skipping rebuild")
        if amdgpu_future is not None:
            amdgpu_future.result()
        with _Timer("dkms build (warm — skipped)") as t:
            t.cold = False
        return str(modules_dir)

    with _Timer("extract kernel headers (no root)"):
        hdrs = glob.glob(f"/tmp/linux-headers-{kver}_*.deb")
        if hdrs and not headers_dir.exists():
            headers_dir.mkdir(parents=True, exist_ok=True)
            run(["dpkg-deb", "--extract", hdrs[0], str(headers_dir)])
        kernelsourcedir = headers_dir / "usr" / "src" / f"linux-headers-{kver}"

    with _Timer("amdgpu source copy") as t:
        if amdgpu_future is not None:
            src_ok = amdgpu_future.result()
        else:
            src_ok = _copy_amdgpu_src(vm_user, scp)
        if not src_ok:
            print("WARNING: amdgpu source not available, skipping DKMS build")
            return None
        if AMDGPU_SRC_HOST.exists() and amdgpu_future is None:
            t.cold = False

    with _Timer("dkms build amdgpu (rootless)"):
        compat_patch = (REPO_DIR / "patches/amdgpu-72x-compat" /
                        "0021-kcl-drm-fb-define-HAVE-fill-info-for-7.2plus.patch")
        run(["patch", "--batch", "-d", str(AMDGPU_SRC_HOST), "-p1",
             "--forward", "--fuzz=3", "--reject-file=/dev/null",
             f"--input={compat_patch}"],
            check=False, stdin=subprocess.DEVNULL)

        # dkms.conf: the package-installed version is at the root; the build
        # tree version is nested at amd/dkms/dkms.conf.  Check root first,
        # then fall back to nested.  If only nested, symlink to root so that
        # dkms add (which always looks at the root) can find it.
        dkms_conf_root = AMDGPU_SRC_HOST / "dkms.conf"
        dkms_conf_nested = AMDGPU_SRC_HOST / "amd/dkms/dkms.conf"
        if not dkms_conf_root.exists() and dkms_conf_nested.exists():
            dkms_conf_root.symlink_to("amd/dkms/dkms.conf")
        dkms_conf = dkms_conf_root if dkms_conf_root.exists() else dkms_conf_nested

        amdgpu_short = AMDGPU_VER.split("-")[0]
        pkg_ver = f"{amdgpu_short}-src"  # fallback
        if dkms_conf.exists():
            for line in dkms_conf.read_text().splitlines():
                if line.startswith("PACKAGE_VERSION="):
                    pkg_ver = line.split("=", 1)[1].strip("\"'")
                    break
        dkms_name = f"amdgpu/{pkg_ver}"
        modules_dir = dkms_tree / "amdgpu" / pkg_ver / kver / "x86_64" / "module"
        print(f"DKMS name: {dkms_name}  tree: {dkms_tree}")

        # Inject ccache into dkms.conf.
        if shutil.which("ccache") and dkms_conf.exists():
            content = dkms_conf.read_text()
            if "ccache" not in content:
                dkms_conf.write_text(re.sub(
                    r"(MAKE\[0\]=\"'make')('.*)",
                    r"\1 CC='ccache gcc'\2",
                    content,
                ))
                print("ccache injected into dkms.conf")

        dkms_tree.mkdir(parents=True, exist_ok=True)
        # dkms --sourcetree DIR expects source at DIR/amdgpu-{pkg_ver}/.
        # Create a user-owned symlink with the expected name if needed.
        source_link = AMDGPU_SRC_HOST.parent / f"amdgpu-{pkg_ver}"
        if not source_link.exists() and source_link != AMDGPU_SRC_HOST:
            source_link.symlink_to(AMDGPU_SRC_HOST)
        # --dkmstree: redirect /var/lib/dkms  --sourcetree: redirect /usr/src
        common = ["--dkmstree", str(dkms_tree),
                  "--sourcetree", str(AMDGPU_SRC_HOST.parent)]
        run(["dkms", "add", *common, dkms_name], check=False)
        run(["dkms", "build", *common, dkms_name, "-k", kver,
             "--kernelsourcedir", str(kernelsourcedir)], check=False)

    return str(modules_dir) if modules_dir.exists() else None


# ---------------------------------------------------------------------------
# Stage 5: Install kernel + DKMS into VM, reboot
# ---------------------------------------------------------------------------

def stage_install_and_reboot(kver: str, modules_dir: str | None,
                              ssh: list[str], scp: list[str]):
    vm_kver = run(ssh + ["uname -r"], capture=True).stdout.strip()
    print(f"VM kernel: {vm_kver}  target: {kver}")
    if vm_kver == kver:
        print(f"VM already running target kernel {kver}, skipping install")
        with _Timer("kernel install + reboot (warm — skipped)") as t:
            t.cold = False
        return

    with _Timer("scp kernel debs + modules to VM"):
        debs = (glob.glob(f"/tmp/linux-image-{kver}_*.deb") +
                glob.glob(f"/tmp/linux-headers-{kver}_*.deb"))
        print(f"Sending debs: {[Path(d).name for d in debs]}")
        if not debs:
            sys.exit(f"No debs found for kver={kver}")
        run([*scp, *debs, f"{ssh[-1]}:/tmp/"])
        if modules_dir:
            ko_files = glob.glob(f"{modules_dir}/*.ko*")
            if ko_files:
                print(f"Sending {len(ko_files)} .ko files")
                run([*scp, *ko_files, f"{ssh[-1]}:/tmp/"])

    guest_script = textwrap.dedent(f"""\
        set -e
        dpkg -i /tmp/linux-image-*.deb /tmp/linux-headers-*.deb
        if ls /tmp/*.ko 2>/dev/null; then
          DST=/usr/lib/modules/{kver}/updates/dkms; mkdir -p $DST
          for ko in /tmp/*.ko; do
            cp $ko $DST/
          done
          depmod -a {kver}
        fi
        # Blacklist the in-tree amdgpu only when we have DKMS overrides in
        # updates/dkms/. Without them (slim VMs), let the in-tree module load.
        if ls /usr/lib/modules/{kver}/updates/dkms/amdgpu.ko 2>/dev/null; then
          echo "blacklist amdgpu" > /usr/lib/modprobe.d/blacklist_linux_{kver}.conf
        else
          # Slim VMs bake modprobe.blacklist=amdgpu into the GRUB cmdline.
          # Strip it from sources; also patch the generated grub.cfg directly.
          sed -i 's/modprobe.blacklist=amdgpu//g' /etc/default/grub || true
          find /etc/default/grub.d/ -name '*.cfg' \
            -exec sed -i 's/modprobe.blacklist=amdgpu//g' {{}} + 2>/dev/null || true
        fi
        echo "options amdgpu emu_mode=1 fw_load_type=0 discovery=2 ip_block_mask=0x7f vm_update_mode=3 gpu_recovery=0 vramlimit=1024" \\
          > /etc/modprobe.d/amdgpu-rocjitsu.conf
        mkdir -p /lib/firmware/amdgpu
        python3 -c "import base64; open('/lib/firmware/amdgpu/ip_discovery.bin','wb').write(base64.b64decode('{IP_DISCOVERY_B64}'))"
        update-initramfs -u -k {kver}
        ROOT_UUID=$(awk '$2=="/" {{gsub("UUID=","", $1); print $1}}' /etc/fstab | head -1)
        echo "GRUB_DEFAULT=\\"gnulinux-advanced-${{ROOT_UUID}}>gnulinux-{kver}-advanced-${{ROOT_UUID}}\\"" \\
          > /etc/default/grub.d/99-pinned-kernel.cfg
        grub-editenv /boot/grub/grubenv unset next_entry
        update-grub
        sed -i 's/ modprobe.blacklist=amdgpu//g' /boot/grub/grub.cfg || true
    """)

    with _Timer("install kernel in VM + reboot"):
        print("Running guest install script...")
        proc = subprocess.Popen(ssh + ["sudo bash -s"], stdin=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        _, stderr = proc.communicate(guest_script.encode())
        if proc.returncode not in (0, None):
            if stderr:
                print(f"Guest stderr:\n{stderr.decode(errors='replace')}")
            sys.exit(f"Guest install script failed (exit {proc.returncode})")
        print("Install complete, rebooting VM...")
        # Reboot separately — drops the SSH connection, so check=False.
        run(ssh + ["sudo reboot now"], check=False)


# ---------------------------------------------------------------------------
# Stage 6: Reconnect after reboot (large-BAR rocjitsu config)
# ---------------------------------------------------------------------------

def stage_reconnect(ssh: list[str]):
    with _Timer("compose down/up after reboot"):
        # Containers may already be stopped after VM reboot; ignore errors.
        run(["qemu-tool", "compose", "--stack", "vfio-user-rocjitsu-vm", "down"],
            env=compose_env(), check=False)
        run(["qemu-tool", "compose", "--stack", "vfio-user-rocjitsu-vm",
             "up", "--detach"], env=compose_env())
        wait_for_socket("vfio-user-rocjitsu-vm-rocjitsu-1",
                        "/run/vfu/rocjitsu-1.sock")

    with _Timer("apply large-BAR rocjitsu config"):
        run(["docker", "stop", "vfio-user-rocjitsu-vm-qemu-1"], check=False)
        patch_cmd = (
            "import json; p='/usr/local/share/rocjitsu/configs/gfx1250_mi455x.json'; "
            "d=json.load(open(p)); g=8*1024**3; "
            "d['vm']['gpu']['device']['local_mem_size']=g; "
            "d['vm']['gpu']['pci']['vram_aperture_bytes']=g; "
            "open(p,'w').write(json.dumps(d,indent=2))"
        )
        run(["docker", "exec", "vfio-user-rocjitsu-vm-rocjitsu-1",
             "python3", "-c", patch_cmd])
        run(["docker", "stop", "vfio-user-rocjitsu-vm-rocjitsu-1"])
        run(["docker", "start", "vfio-user-rocjitsu-vm-rocjitsu-1"])
        wait_for_socket("vfio-user-rocjitsu-vm-rocjitsu-1",
                        "/run/vfu/rocjitsu-1.sock")
        run(["docker", "start", "vfio-user-rocjitsu-vm-qemu-1"], check=False)
        wait_for_ssh(ssh)

    kver = run(ssh + ["uname -r"], capture=True).stdout.strip()
    print(f"Booted: {kver}")


# ---------------------------------------------------------------------------
# Stage 7: Load amdgpu + verify
# ---------------------------------------------------------------------------

def stage_load_amdgpu(ssh: list[str]):
    with _Timer("amdgpu probe"):
        # When no DKMS modules are installed, the VM may have booted with
        # modprobe.blacklist=amdgpu in the kernel cmdline (set by grub in the
        # slim VM). Override this via modprobe.d --ignore-install so the
        # in-tree amdgpu can load regardless of the cmdline blacklist.
        amdgpu_opts = ("emu_mode=1 fw_load_type=0 discovery=2 "
                       "ip_block_mask=0x7f vm_update_mode=3 "
                       "gpu_recovery=0 vramlimit=1024")
        run(ssh + [
            "sudo bash -c "
            f"'echo \"install amdgpu /sbin/modprobe --ignore-install amdgpu {amdgpu_opts}\" "
            "> /etc/modprobe.d/99-force-amdgpu.conf'"
        ], check=False)
        run(ssh + ["sudo /usr/local/bin/amdgpu-probe"])
        time.sleep(8)
        run(ssh + ["ls /dev/kfd /dev/dri/renderD128"])
        # NVMe is emulated by rocjitsu and may take a moment after probe.
        print("Waiting for NVMe device...")
        for i in range(30):
            r = run(ssh + ["lsblk -d -o NAME,SIZE | grep -qi nvme"],
                    check=False)
            if r.returncode == 0:
                r2 = run(ssh + ["lsblk -d -o NAME,SIZE"], capture=True)
                print(f"Block devices:\n{r2.stdout.strip()}")
                break
            if i % 5 == 4:
                r2 = run(ssh + ["lsblk -d -o NAME,SIZE"], capture=True,
                         check=False)
                print(f"  ({i+1}s) lsblk:\n{r2.stdout.strip()}")
            time.sleep(1)
        else:
            r2 = run(ssh + ["lsblk -d -o NAME,SIZE"], capture=True,
                     check=False)
            sys.exit(f"NVMe not visible after 30s. lsblk:\n{r2.stdout.strip()}")


# ---------------------------------------------------------------------------
# Stage 8: Run P2P tests
# ---------------------------------------------------------------------------

def stage_p2p_tests(ssh: list[str], scp: list[str]):
    test_dir = REPO_DIR / "userspace/amdgpu-dmabuf-io"
    binary = Path("/tmp/test_p2p_io")

    with _Timer("build test binary") as t:
        if binary.exists():
            print(f"Using pre-built {binary}")
            t.cold = False
        else:
            # Try to build on host if hipcc is available.
            hipcc = (shutil.which("hipcc")
                     or next((str(p) for p in [
                         Path("/opt/rocm/bin/hipcc"),
                         *sorted(Path("/opt/rocm").glob("core-*/bin/hipcc")),
                     ] if Path(p).exists()), None))
            if hipcc:
                run(["make", f"CXX={hipcc}"], cwd=test_dir, check=False)
                built = test_dir / "test_p2p_io"
                if built.exists():
                    shutil.copy(built, binary)
            if not binary.exists():
                # hipcc absent (CI runner without ROCm) — build inside the VM.
                print("hipcc not on host; building test binary inside VM...")
                run([*scp, "-r", str(test_dir), f"{ssh[-1]}:/tmp/"])
                build_script = textwrap.dedent("""\
                    set -e
                    ROCM=$(ls -d /opt/rocm/core-* 2>/dev/null | sort -V | tail -1)
                    cd /tmp/amdgpu-dmabuf-io
                    make CXX=${ROCM}/bin/hipcc \
                         CXXFLAGS="-O3 -I/usr/include/libdrm -I${ROCM}/include"
                """)
                proc = subprocess.Popen(ssh + ["sudo bash -s"],
                                        stdin=subprocess.PIPE)
                proc.communicate(build_script.encode())
                if proc.returncode != 0:
                    sys.exit("Failed to build test binary in VM")
                # Pull the binary back to host for caching.
                run([*scp, f"{ssh[-1]}:/tmp/amdgpu-dmabuf-io/test_p2p_io",
                     str(binary)], check=False)

    with _Timer("scp test binary"):
        run([*scp, str(binary), f"{ssh[-1]}:/tmp/"])

    test_script = textwrap.dedent("""\
        set -e
        sudo mkdir -p /var/log/dmabuf-test

        echo "=== raw NVMe ==="
        /tmp/test_p2p_io /dev/nvme0n1 2>&1 | tee /var/log/dmabuf-test/p2p-raw.log
        grep "Success" /var/log/dmabuf-test/p2p-raw.log

        echo "=== XFS file ==="
        sudo modprobe xfs
        sudo mkfs.xfs -f /dev/nvme0n1
        sudo mkdir -p /mnt/nvme && sudo mount /dev/nvme0n1 /mnt/nvme
        sudo dd if=/dev/zero of=/mnt/nvme/test.bin bs=4M count=1 status=none
        /tmp/test_p2p_io /mnt/nvme/test.bin 2>&1 | tee /var/log/dmabuf-test/p2p-xfs.log
        grep "Success" /var/log/dmabuf-test/p2p-xfs.log
        sudo umount /mnt/nvme
        echo "Both tests passed"
    """)

    with _Timer("P2P tests (raw NVMe + XFS)"):
        proc = subprocess.Popen(ssh + ["sudo bash -s"], stdin=subprocess.PIPE)
        proc.communicate(test_script.encode())
        if proc.returncode != 0:
            sys.exit(f"P2P tests failed (exit {proc.returncode})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def do_clean(full: bool = False):
    """Remove cached artifacts.

    full=False (--clean): kernel debs, .config, vm-lsmod, orig.tar.gz.
    full=True  (--clean-full): everything above + backing qcow2 and VM metadata.
    """
    removed = []
    # Always clean kernel build artifacts
    for pattern in [
        f"/tmp/linux-image-*{KERNEL_LABEL}*.deb",
        f"/tmp/linux-headers-*{KERNEL_LABEL}*.deb",
        str(KERNEL_SRC.parent / "linux-upstream_*.orig.tar.gz"),
    ]:
        for f in glob.glob(pattern):
            Path(f).unlink()
            removed.append(f)
    for p in [KERNEL_SRC / ".config", KERNEL_SRC / "vm-lsmod",
              Path("/tmp/test_p2p_io")]:
        if p.exists():
            p.unlink()
            removed.append(str(p))
    # --clean: remove DKMS builds matching the current kernel label.
    # --clean-full: remove the entire /tmp/dkms-build tree.
    dkms_tree = Path("/tmp/dkms-build")
    if full and dkms_tree.exists():
        shutil.rmtree(dkms_tree, ignore_errors=True)
        removed.append(str(dkms_tree))
    elif dkms_tree.exists():
        for kver_dir in dkms_tree.glob(f"amdgpu/*/{'{'}*{KERNEL_LABEL}*{'}'}"):
            shutil.rmtree(kver_dir, ignore_errors=True)
            removed.append(str(kver_dir))
    # Also clean extracted headers for any of our kernel builds.
    for hdr_dir in Path("/tmp").glob(f"headers-*{KERNEL_LABEL}*"):
        shutil.rmtree(hdr_dir, ignore_errors=True)
        removed.append(str(hdr_dir))
    # Remove amdgpu source copy so the SCP from the VM is retried fresh.
    if AMDGPU_SRC_HOST.exists():
        shutil.rmtree(AMDGPU_SRC_HOST, ignore_errors=True)
        removed.append(str(AMDGPU_SRC_HOST))
    if full:
        # Tear down the VM first to release qcow2 file locks.
        print("Tearing down VM before removing images...")
        run(["qemu-tool", "compose", "--stack", "vfio-user-rocjitsu-vm", "down"],
            env=compose_env(), check=False)
        for p in [
            VM_IMAGES_DIR / f"{VM_NAME}-backing.qcow2",
            VM_IMAGES_DIR / "vm-info.json",
            VM_IMAGES_DIR / "id_rsa",
            VM_IMAGES_DIR / "id_rsa.pub",
            VM_IMAGES_DIR / "referrers.json",
        ]:
            if p.exists():
                p.unlink()
                removed.append(str(p))
    if removed:
        print("Removed:")
        for r in removed:
            print(f"  {r}")
    else:
        print("Nothing to clean.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-kernel-build", action="store_true",
                        help="Assume kernel debs are already in /tmp")
    parser.add_argument("--skip-dkms", action="store_true",
                        help="Assume DKMS modules already built")
    parser.add_argument("--clean", action="store_true",
                        help="Remove kernel debs/.config/orig.tar.gz then continue")
    parser.add_argument("--clean-full", action="store_true",
                        help="--clean plus backing qcow2 and VM metadata then continue")
    args = parser.parse_args()

    if args.clean_full:
        do_clean(full=True)
    elif args.clean:
        do_clean(full=False)

    VM_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    total_start = time.monotonic()

    vm_user = stage_setup_vm()
    ssh, scp = stage_boot(vm_user)

    # Kick off amdgpu source copy in parallel with the kernel build.
    with ThreadPoolExecutor(max_workers=1) as pool:
        if not AMDGPU_SRC_HOST.exists():
            print("Starting parallel amdgpu source copy from VM...")
            amdgpu_future = pool.submit(_copy_amdgpu_src, vm_user, scp)
        else:
            amdgpu_future = None

        kver = stage_build_kernel(ssh, scp)

    modules_dir = stage_build_dkms(kver, vm_user, scp, amdgpu_future)
    stage_install_and_reboot(kver, modules_dir, ssh, scp)

    vm_kver = run(ssh + ["uname -r"], capture=True, check=False).stdout.strip()
    if vm_kver != kver:
        stage_reconnect(ssh)
    else:
        print(f"VM already on {kver}, skipping reboot reconnect")
        with _Timer("reboot reconnect (warm — skipped)") as t:
            t.cold = False

    stage_load_amdgpu(ssh)
    stage_p2p_tests(ssh, scp)

    print(f"\nVM: ssh -i {VM_IMAGES_DIR}/id_rsa -p {VM_SSH_PORT} "
          f"{vm_user}@{VM_SSH_HOST}")
    print_timing_summary()
    print(f"\nTotal wall-clock: {time.monotonic() - total_start:.1f}s")


if __name__ == "__main__":
    main()
