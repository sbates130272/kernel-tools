"""dmabuf-test — DMA-BUF P2P test: rocjitsu GPU + NVMe VM, 7.2.x backport kernel.

Python port of run-this.py (originally run-this.sh).  Key features:
  - Parallel: amdgpu source copy from VM overlaps kernel build
  - ccache wraps the kernel build (CC/CXX) for fast incremental rebuilds
  - DKMS build skipped when built .ko files already exist for KVER
  - Kernel install + reboot skipped when VM already runs the target kernel
  - amdgpu source scp skipped when /tmp/amdgpu-<ver>-src already exists
  - Per-stage wall-clock timing printed at end (cold and warm path annotated)

Prerequisites: oras, qemu-tool, docker, kernel build tools,
               ccache (optional but recommended)
"""

from __future__ import annotations

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

from ktool._util import run

# ---------------------------------------------------------------------------
# Defaults (all overridable via CLI or environment)
# ---------------------------------------------------------------------------

_QCOW2_IMAGE = (
    "docker.io/sbates130272/"
    "batesste-ci-images-ubuntu-qcow2-gen-ernic-rocjitsu-slim:"
    "20261022.g1177df0-vm.resolute-ernic-rocjitsu-slim-qm.c079195-qcow2"
)
_ROCJITSU_IMAGE = (
    "docker.io/sbates130272/"
    "batesste-ci-images-ubuntu-rocm-rocjitsu:"
    "20261022.g1177df0-rocjitsu.98f6a72"
)
_QEMU_IMAGE = (
    "docker.io/sbates130272/"
    "batesste-ci-images-ubuntu-qemu-libvfio-user-sbates-fork:"
    "20261022.g1177df0-qemu.f91f120-vfu.3719c3a"
)

# Verbatim IP discovery blob — do not reformat.
_IP_DISCOVERY_B64 = (
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


def _print_timing_summary() -> None:
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

def _ssh_args(images_dir: Path, port: int, host: str, user: str) -> list[str]:
    return [
        "ssh",
        "-i", str(images_dir / "id_rsa"),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "-p", str(port),
        f"{user}@{host}",
    ]


def _scp_args(images_dir: Path, port: int) -> list[str]:
    return [
        "scp",
        "-i", str(images_dir / "id_rsa"),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "-P", str(port),
    ]


def _vm_is_up(vm_name: str) -> bool:
    r = run(["qemu-tool", "inspect", "--vm-name", vm_name],
            check=False, capture=True)
    return r.returncode == 0


def _wait_for_ssh(ssh: list[str], *, timeout: int = 300, interval: int = 3) -> None:
    start = time.monotonic()
    deadline = start + timeout
    attempt = 0
    while time.monotonic() < deadline:
        r = run(ssh + ["true"], check=False, capture=True,
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


def _wait_for_socket(container: str, sock: str, *, retries: int = 30) -> None:
    for _ in range(retries):
        r = run(["docker", "exec", container, "test", "-S", sock],
                check=False, capture=True)
        if r.returncode == 0:
            return
        time.sleep(1)


def _compose_env(cfg: "Config") -> dict[str, str]:
    return {
        "VM_NAME":       cfg.vm_name,
        "VM_IMAGES_DIR": str(cfg.images_dir),
        "VM_SSH_PORT":   str(cfg.ssh_port),
        "VM_NVME":       "1",
        "VM_VCPUS":      str(cfg.vcpus),
        "VM_MEM":        str(cfg.mem),
        "ROCJITSU_IMAGE": cfg.rocjitsu_image,
        "QEMU_IMAGE":    cfg.qemu_image,
    }


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, args: argparse.Namespace, repo_dir: Path):
        self.repo_dir       = repo_dir
        self.images_dir     = Path(args.images_dir)
        self.ssh_port       = args.ssh_port
        self.ssh_host       = args.ssh_host
        self.vm_name        = args.vm_name
        self.vcpus          = args.vcpus
        self.mem            = args.mem
        self.kernel_label   = args.kernel_label
        self.kernel_branch  = args.kernel_branch
        self.kernel_src     = Path(args.kernel_src)
        self.amdgpu_ver     = args.amdgpu_ver
        self.amdgpu_src_vm  = f"/usr/src/amdgpu-{args.amdgpu_ver}"
        self.amdgpu_src_host = Path(
            f"/tmp/amdgpu-{args.amdgpu_ver.split('-')[0]}-src")
        self.qcow2_image    = args.qcow2_image
        self.rocjitsu_image = args.rocjitsu_image
        self.qemu_image     = args.qemu_image


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _stage_setup_vm(cfg: Config) -> str:
    with _Timer("teardown existing VM") as t:
        if _vm_is_up(cfg.vm_name):
            print("VM already up, tearing down...")
        else:
            t.cold = False
        run(["qemu-tool", "compose", "--stack", "vfio-user-rocjitsu-vm", "down"],
            env=_compose_env(cfg), check=False)

    backing  = cfg.images_dir / f"{cfg.vm_name}-backing.qcow2"
    vm_info  = cfg.images_dir / "vm-info.json"
    id_rsa   = cfg.images_dir / "id_rsa"

    with _Timer("fetch OCI metadata") as t:
        if vm_info.exists() and id_rsa.exists() and backing.exists():
            print(f"Using cached VM image: {backing}")
            t.cold = False
        else:
            refs = json.loads(
                run(["oras", "discover", "--format", "json", cfg.qcow2_image],
                    capture=True).stdout)
            manifests = refs.get("manifests", refs.get("referrers", []))
            ref = next(
                r["digest"] for r in manifests
                if r.get("artifactType") == "application/vnd.batesste.vm-info.v1"
            )
            registry = cfg.qcow2_image.split(":")[0]
            run(["oras", "pull", "--output", str(cfg.images_dir),
                 f"{registry}@{ref}"])

    with _Timer("gen-vm overlay"):
        run(["qemu-tool", "gen-vm",
             "--vm-name", cfg.vm_name,
             "--images",  str(cfg.images_dir),
             "--backing-image", cfg.qcow2_image,
             "--no-compact"])

    id_rsa.chmod(0o600)
    return json.loads(vm_info.read_text()).get("username", "ubuntu")


def _stage_boot(cfg: Config, vm_user: str) -> tuple[list[str], list[str]]:
    ssh = _ssh_args(cfg.images_dir, cfg.ssh_port, cfg.ssh_host, vm_user)
    scp = _scp_args(cfg.images_dir, cfg.ssh_port)

    with _Timer("compose up + VM ready"):
        run(["qemu-tool", "compose", "--stack", "vfio-user-rocjitsu-vm",
             "up", "--detach"], env=_compose_env(cfg))
        _wait_for_socket("vfio-user-rocjitsu-vm-rocjitsu-1",
                         "/run/vfu/rocjitsu-1.sock")
        _wait_for_ssh(ssh)

    kver = run(ssh + ["uname -r"], capture=True).stdout.strip()
    print(f"VM ready (stock kernel): {kver}")
    return ssh, scp


def _stage_build_kernel(cfg: Config, ssh: list[str], scp: list[str]) -> str:
    deb_pattern = f"/tmp/linux-image-*{cfg.kernel_label}*.deb"
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
        if cfg.kernel_src.exists():
            t.cold = False
        else:
            run(["git", "clone", "--depth", "1",
                 "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
                 "-b", cfg.kernel_branch, cfg.kernel_src.name],
                cwd=cfg.kernel_src.parent)

    patches = sorted(
        (cfg.repo_dir / "patches/dmabuf-72x-backport").glob("*.patch"))
    base = run(["git", "rev-list", "--max-parents=0", "HEAD"],
               cwd=cfg.kernel_src, capture=True).stdout.strip()
    applied = int(run(["git", "rev-list", "--count", f"{base}..HEAD"],
                      cwd=cfg.kernel_src, capture=True).stdout.strip())

    run(["git", "am", "--abort"], cwd=cfg.kernel_src, check=False)
    run(["git", "reset", "--hard", "HEAD"], cwd=cfg.kernel_src)
    run(["git", "clean", "-fd"], cwd=cfg.kernel_src)

    with _Timer("apply patches") as t:
        if applied == len(patches):
            print(f"Patches already applied ({len(patches)}), skipping")
            t.cold = False
        else:
            run(["git", "reset", "--hard", base], cwd=cfg.kernel_src)
            run(["git", "clean", "-fd"], cwd=cfg.kernel_src)
            run(["git", "am", *[str(p) for p in patches]], cwd=cfg.kernel_src)
            for stale in glob.glob(
                    str(cfg.kernel_src.parent / "linux-upstream_*.orig.tar.gz")):
                os.unlink(stale)

    config_file = cfg.kernel_src / ".config"
    with _Timer("configure kernel (build-remote style)") as t:
        if config_file.exists() and applied == len(patches):
            print("Kernel .config already present, skipping configure")
            t.cold = False
        else:
            vm_kver = run(ssh + ["uname -r"], capture=True).stdout.strip()
            print(f"Pulling VM config for {vm_kver}...")
            run([*scp, f"{ssh[-1]}:/boot/config-{vm_kver}", str(config_file)])
            lsmod_file = cfg.kernel_src / "vm-lsmod"
            lsmod_file.write_text(run(ssh + ["lsmod"], capture=True).stdout)
            for flag in ("SYSTEM_TRUSTED_KEYRING", "SYSTEM_TRUSTED_KEYS",
                         "SYSTEM_REVOCATION_KEYS"):
                run(["scripts/config", "--disable", flag], cwd=cfg.kernel_src)
            run(["make", "olddefconfig"], cwd=cfg.kernel_src)
            run(["scripts/config", "--disable", "GPIO_BT8XX",
                 "--module", "BLK_DEV_NULL_BLK"], cwd=cfg.kernel_src)
            run(["make", f"LSMOD={lsmod_file}", "localmodconfig"],
                cwd=cfg.kernel_src)
            # Find config-amdgpu-dkms from repo root or installed package
            config_script = cfg.repo_dir / "config-scripts/config-amdgpu-dkms"
            if not config_script.exists():
                config_script = Path(__file__).parent / "config_scripts/config-amdgpu-dkms"
            run(["scripts/config",
                 "--enable",  "LOCALVERSION_AUTO",
                 "--set-str", "LOCALVERSION", cfg.kernel_label,
                 "--enable",  "DEBUG_INFO_NONE",
                 "--disable", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT",
                 "--disable", "DEBUG_INFO_DWARF4",
                 "--disable", "DEBUG_INFO_DWARF5"], cwd=cfg.kernel_src)
            if config_script.exists():
                run(["bash", "-c", f"source {config_script}"], cwd=cfg.kernel_src)
            run(["make", "olddefconfig"], cwd=cfg.kernel_src)

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
            cwd=cfg.kernel_src, env=ccache_env or None)

    return run(["make", "-s", "kernelrelease"], cwd=cfg.kernel_src,
               capture=True).stdout.strip()


def _copy_amdgpu_src(cfg: Config, vm_user: str, scp: list[str]) -> bool:
    if cfg.amdgpu_src_host.exists():
        print(f"amdgpu source already at {cfg.amdgpu_src_host}, skipping scp")
        return True
    r = run([*scp, "-r",
             f"{vm_user}@{cfg.ssh_host}:{cfg.amdgpu_src_vm}",
             str(cfg.amdgpu_src_host)],
            check=False, capture=True, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def _stage_build_dkms(cfg: Config, kver: str, vm_user: str, scp: list[str],
                      amdgpu_future=None) -> str | None:
    amdgpu_short = cfg.amdgpu_ver.split("-")[0]
    modules_dir = Path(
        f"/var/lib/dkms/amdgpu/{amdgpu_short}-src/{kver}/x86_64/module")

    with _Timer("install kernel headers"):
        hdrs = glob.glob(f"/tmp/linux-headers-{kver}_*.deb")
        if hdrs:
            run(["dpkg", "-i", *hdrs], sudo=True, check=False)

    with _Timer("amdgpu source copy") as t:
        if amdgpu_future is not None:
            src_ok = amdgpu_future.result()
        else:
            src_ok = _copy_amdgpu_src(cfg, vm_user, scp)
        if not src_ok:
            print("WARNING: amdgpu source not available, skipping DKMS build")
            return None
        if cfg.amdgpu_src_host.exists() and amdgpu_future is None:
            t.cold = False

    ko_files = list(modules_dir.glob("*.ko*")) if modules_dir.exists() else []
    if ko_files:
        print(f"DKMS modules already built for {kver}, skipping")
        with _Timer("dkms build (warm — skipped)") as t:
            t.cold = False
        return str(modules_dir)

    with _Timer("dkms build amdgpu"):
        run(["ln", "-sfn", str(cfg.amdgpu_src_host),
             f"/usr/src/amdgpu-{amdgpu_short}-src"], sudo=True)
        compat_patch = (
            cfg.repo_dir / "patches/amdgpu-72x-compat" /
            "0021-kcl-drm-fb-define-HAVE-fill-info-for-7.2plus.patch"
        )
        if compat_patch.exists():
            run(["patch", "-d", str(cfg.amdgpu_src_host), "-p1",
                 "--forward", "--fuzz=3", f"--input={compat_patch}"],
                sudo=True, check=False)
        dkms_conf = cfg.amdgpu_src_host / "amd/dkms/dkms.conf"
        if shutil.which("ccache") and dkms_conf.exists():
            import re as _re
            content = dkms_conf.read_text()
            if "ccache" not in content:
                patched = _re.sub(
                    r"(MAKE\[0\]=\"'make')('.*)",
                    r"\1 CC='ccache gcc'\2",
                    content,
                )
                dkms_conf.write_text(patched)
                print("ccache injected into dkms.conf")
        dkms_name = f"amdgpu/{amdgpu_short}-src"
        run(["dkms", "add", dkms_name], sudo=True, check=False)
        run(["dkms", "build", dkms_name, "-k", kver,
             "--kernelsourcedir", f"/usr/src/linux-headers-{kver}"],
            sudo=True, check=False)

    return str(modules_dir) if modules_dir.exists() else None


def _stage_install_and_reboot(cfg: Config, kver: str, modules_dir: str | None,
                               ssh: list[str], scp: list[str]) -> None:
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
        echo "blacklist amdgpu" > /usr/lib/modprobe.d/blacklist_linux_{kver}.conf
        echo "options amdgpu emu_mode=1 fw_load_type=0 discovery=2 ip_block_mask=0x7f vm_update_mode=3 gpu_recovery=0 vramlimit=1024" \\
          > /etc/modprobe.d/amdgpu-rocjitsu.conf
        mkdir -p /lib/firmware/amdgpu
        python3 -c "import base64; open('/lib/firmware/amdgpu/ip_discovery.bin','wb').write(base64.b64decode('{_IP_DISCOVERY_B64}'))"
        update-initramfs -u -k {kver}
        ROOT_UUID=$(awk '$2=="/" {{gsub("UUID=","", $1); print $1}}' /etc/fstab | head -1)
        echo "GRUB_DEFAULT=\\"gnulinux-advanced-${{ROOT_UUID}}>gnulinux-{kver}-advanced-${{ROOT_UUID}}\\"" \\
          > /etc/default/grub.d/99-pinned-kernel.cfg
        grub-editenv /boot/grub/grubenv unset next_entry
        update-grub
    """)

    with _Timer("install kernel in VM + reboot"):
        proc = subprocess.Popen(ssh + ["sudo bash -s"], stdin=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        _, stderr = proc.communicate(guest_script.encode())
        if proc.returncode not in (0, None):
            if stderr:
                print(f"Guest stderr:\n{stderr.decode(errors='replace')}")
            sys.exit(f"Guest install script failed (exit {proc.returncode})")
        run(ssh + ["sudo reboot now"], check=False)


def _stage_reconnect(cfg: Config, ssh: list[str]) -> None:
    with _Timer("compose down/up after reboot"):
        run(["qemu-tool", "compose", "--stack", "vfio-user-rocjitsu-vm", "down"],
            env=_compose_env(cfg), check=False)
        run(["qemu-tool", "compose", "--stack", "vfio-user-rocjitsu-vm",
             "up", "--detach"], env=_compose_env(cfg))
        _wait_for_socket("vfio-user-rocjitsu-vm-rocjitsu-1",
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
        _wait_for_socket("vfio-user-rocjitsu-vm-rocjitsu-1",
                         "/run/vfu/rocjitsu-1.sock")
        run(["docker", "start", "vfio-user-rocjitsu-vm-qemu-1"], check=False)
        _wait_for_ssh(ssh)

    kver = run(ssh + ["uname -r"], capture=True).stdout.strip()
    print(f"Booted: {kver}")


def _stage_load_amdgpu(ssh: list[str]) -> None:
    with _Timer("amdgpu probe"):
        run(ssh + ["sudo /usr/local/bin/amdgpu-probe"])
        time.sleep(8)
        run(ssh + ["ls /dev/kfd /dev/dri/renderD128"])
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
            r2 = run(ssh + ["lsblk -d -o NAME,SIZE"], capture=True, check=False)
            sys.exit(f"NVMe not visible after 30s. lsblk:\n{r2.stdout.strip()}")


def _stage_p2p_tests(cfg: Config, ssh: list[str], scp: list[str]) -> None:
    test_dir = cfg.repo_dir / "userspace/amdgpu-dmabuf-io"
    binary   = Path("/tmp/test_p2p_io")

    with _Timer("build test binary on host") as t:
        if binary.exists():
            print(f"Using pre-built {binary}")
            t.cold = False
        else:
            hipcc = shutil.which("hipcc") or "/opt/rocm/bin/hipcc"
            run(["make", f"CXX={hipcc}", f"OUTPUT={binary}"],
                cwd=test_dir, check=False)
            if not binary.exists():
                run(["make", f"CXX={hipcc}"], cwd=test_dir)
                shutil.copy(test_dir / "test_p2p_io", binary)

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


def _do_clean(cfg: Config, full: bool = False) -> None:
    removed = []
    for pattern in [
        f"/tmp/linux-image-*{cfg.kernel_label}*.deb",
        f"/tmp/linux-headers-*{cfg.kernel_label}*.deb",
        str(cfg.kernel_src.parent / "linux-upstream_*.orig.tar.gz"),
    ]:
        for f in glob.glob(pattern):
            Path(f).unlink()
            removed.append(f)
    for p in [cfg.kernel_src / ".config", cfg.kernel_src / "vm-lsmod",
              Path("/tmp/test_p2p_io")]:
        if p.exists():
            p.unlink()
            removed.append(str(p))
    amdgpu_short = cfg.amdgpu_ver.split("-")[0]
    dkms_base = Path(f"/var/lib/dkms/amdgpu/{amdgpu_short}-src")
    if dkms_base.exists():
        for d in dkms_base.iterdir():
            if cfg.kernel_label in d.name:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(str(d))
    if full:
        for p in [
            cfg.images_dir / f"{cfg.vm_name}-backing.qcow2",
            cfg.images_dir / "vm-info.json",
            cfg.images_dir / "id_rsa",
            cfg.images_dir / "id_rsa.pub",
            cfg.images_dir / "referrers.json",
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


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------

def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("dmabuf-test",
                        help="DMA-BUF P2P test: rocjitsu GPU + NVMe VM")
    p.add_argument("--images-dir",
                   default=os.environ.get("VM_IMAGES_DIR",
                                          "/var/lib/qemu-tool/images"),
                   help="VM images directory")
    p.add_argument("--ssh-port", type=int,
                   default=int(os.environ.get("VM_SSH_PORT", "2232")))
    p.add_argument("--ssh-host",
                   default=os.environ.get("VM_SSH_HOST", "localhost"))
    p.add_argument("--vm-name", default="dmabuf-test")
    p.add_argument("--vcpus", type=int,
                   default=int(os.environ.get("VM_VCPUS", "8")))
    p.add_argument("--mem", type=int,
                   default=int(os.environ.get("VM_MEM", "16384")))
    p.add_argument("--kernel-label", default="-dmabuf-72x")
    p.add_argument("--kernel-branch",
                   default=os.environ.get("KERNEL_BRANCH", "v7.2.4"))
    p.add_argument("--kernel-src",
                   default=os.environ.get("KERNEL_SRC", "/tmp/linux-dmabuf"))
    p.add_argument("--amdgpu-ver", default="7.1.9-2407772.26.04")
    p.add_argument("--qcow2-image", default=_QCOW2_IMAGE)
    p.add_argument("--rocjitsu-image", default=_ROCJITSU_IMAGE)
    p.add_argument("--qemu-image", default=_QEMU_IMAGE)
    p.add_argument("--skip-kernel-build", action="store_true")
    p.add_argument("--skip-dkms", action="store_true")
    p.add_argument("--clean", action="store_true",
                   help="Remove kernel debs/.config then continue")
    p.add_argument("--clean-full", action="store_true",
                   help="--clean plus backing qcow2 and VM metadata then continue")
    p.set_defaults(func=run_cmd)


def run_cmd(args: argparse.Namespace) -> int:
    # Resolve repo dir: works whether run from repo root or as installed package.
    repo_dir = Path(__file__).resolve().parent.parent.parent
    if not (repo_dir / "patches").exists():
        repo_dir = Path.cwd()

    cfg = Config(args, repo_dir)

    if args.clean_full:
        _do_clean(cfg, full=True)
    elif args.clean:
        _do_clean(cfg, full=False)

    cfg.images_dir.mkdir(parents=True, exist_ok=True)
    total_start = time.monotonic()

    vm_user = _stage_setup_vm(cfg)
    ssh, scp = _stage_boot(cfg, vm_user)

    with ThreadPoolExecutor(max_workers=1) as pool:
        if not cfg.amdgpu_src_host.exists():
            print("Starting parallel amdgpu source copy from VM...")
            amdgpu_future = pool.submit(_copy_amdgpu_src, cfg, vm_user, scp)
        else:
            amdgpu_future = None

        if args.skip_kernel_build:
            kver = run(ssh + ["uname -r"], capture=True).stdout.strip()
            print(f"--skip-kernel-build: using VM kernel {kver}")
        else:
            kver = _stage_build_kernel(cfg, ssh, scp)

    if not args.skip_dkms:
        modules_dir = _stage_build_dkms(cfg, kver, vm_user, scp, amdgpu_future)
    else:
        modules_dir = None

    _stage_install_and_reboot(cfg, kver, modules_dir, ssh, scp)

    vm_kver = run(ssh + ["uname -r"], capture=True, check=False).stdout.strip()
    if vm_kver != kver:
        _stage_reconnect(cfg, ssh)
    else:
        print(f"VM already on {kver}, skipping reboot reconnect")
        with _Timer("reboot reconnect (warm — skipped)") as t:
            t.cold = False

    _stage_load_amdgpu(ssh)
    _stage_p2p_tests(cfg, ssh, scp)

    print(f"\nVM: ssh -i {cfg.images_dir}/id_rsa -p {cfg.ssh_port} "
          f"{vm_user}@{cfg.ssh_host}")
    _print_timing_summary()
    print(f"\nTotal wall-clock: {time.monotonic() - total_start:.1f}s")
    return 0
