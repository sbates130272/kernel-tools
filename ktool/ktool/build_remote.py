"""build-remote — build a localmodconfig kernel targeting a remote or local VM.

Ports scripts/build-remote.
Must be called from the top-level of a kernel source tree.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

from ktool._util import run, die

# Config script fragments shipped alongside the package.
# In the installed package they live in ktool/config_scripts/; at development
# time they live in config-scripts/ at the repo root.  The subcommand resolves
# whichever is present.
_CONFIG_SCRIPTS: dict[str, str] = {
    "default":     "config-default",
    "amdgpu":      "config-amdgpu",
    "amdgpu-dkms": "config-amdgpu-dkms",
    "infiniband":  "config-infiniband",
    "nvme-target": "config-nvme-target",
    "wsl":         "config-wsl",
}


def _find_config_script(name: str) -> Path | None:
    """Locate a config-script by name relative to the kernel tree or package."""
    # Try repo root first (development), then installed package data.
    candidates = [
        Path(__file__).parent.parent / "config-scripts" / _CONFIG_SCRIPTS.get(name, name),
        Path(__file__).parent / "config_scripts" / _CONFIG_SCRIPTS.get(name, name),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _ssh(remote: str, cmd: str) -> subprocess.CompletedProcess:
    return run(["ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", remote, cmd],
               capture=True)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("build-remote",
                        help="Build a localmodconfig kernel for a remote host/VM")
    p.add_argument("--remote-name", default="none",
                   help="SSH hostname of the target (or 'none' for localhost, "
                        "'localhost' for local build)")
    p.add_argument("--config", default="none",
                   help="Running kernel config name on the remote "
                        "(e.g. config-6.8.0-60-generic)")
    p.add_argument("--config-script", default="none",
                   choices=list(_CONFIG_SCRIPTS.keys()) + ["none"],
                   help="Extra config fragment to apply (default: none)")
    p.add_argument("--label", default="-build-remote",
                   help="LOCALVERSION suffix (default: -build-remote)")
    p.add_argument("--no-build", action="store_true",
                   help="Stop after clean + setup (no make)")
    p.add_argument("--no-install", action="store_true",
                   help="Build but do not install or reboot")
    p.add_argument("--wsl-mode", action="store_true",
                   help="Build for WSL2 (targets localhost, no remote needed)")
    p.add_argument("--win-user", default="stebates",
                   help="Windows username for WSL kernel dir (default: stebates)")
    p.set_defaults(func=run_cmd)


def run_cmd(args: argparse.Namespace) -> int:
    remote = args.remote_name
    label  = args.label
    wsl    = args.wsl_mode

    wsl_kernel_dir = Path("/mnt/c/wsl-kernels")
    wsl_config     = Path(f"/mnt/c/Users/{args.win_user}/.wslconfig")

    # 1. Resolve the base .config from the remote host (or locally for WSL).
    if wsl:
        # For WSL we copy from /proc/config.gz of the current WSL kernel.
        run(["bash", "-c",
             "zcat /proc/config.gz > .config || "
             "cp /boot/config-$(uname -r) .config"])
    elif remote == "localhost":
        # localhost: use --config as a local file path, or fall back to running kernel.
        if args.config not in ("none", ""):
            import shutil
            shutil.copy(args.config, ".config")
        else:
            run(["bash", "-c",
                 "cp /boot/config-$(uname -r) .config"])
    elif remote != "none":
        vm_config = args.config
        if vm_config == "none":
            vm_config = _ssh(remote, "ls /boot/config-$(uname -r)").stdout.strip()
            vm_config = vm_config.replace("/boot/", "")
        print(f"Pulling config {vm_config} from {remote}...")
        run(["scp", f"{remote}:/boot/{vm_config}", ".config"])

        print(f"Pulling lsmod from {remote}...")
        lsmod_out = _ssh(remote, "lsmod").stdout
        Path("lsmod-remote").write_text(lsmod_out)

    # 2. Disable keys that cause build failures.
    for flag in ("SYSTEM_TRUSTED_KEYRING", "SYSTEM_TRUSTED_KEYS",
                 "SYSTEM_REVOCATION_KEYS"):
        run(["scripts/config", "--disable", flag], check=False)

    run(["make", "olddefconfig"])

    # 3. localmodconfig
    if not wsl and remote not in ("none", "localhost"):
        run(["make", "LSMOD=lsmod-remote", "localmodconfig"])
    else:
        run(["make", "localmodconfig"])

    # 4. Apply optional config script fragment.
    if args.config_script != "none":
        script = _find_config_script(args.config_script)
        if script is None:
            die(f"Config script '{args.config_script}' not found")
        run(["bash", "-c", f"source {script}"])

    # 5. Set LOCALVERSION / debug info off.
    run(["scripts/config",
         "--enable",  "LOCALVERSION_AUTO",
         "--set-str", "LOCALVERSION", label,
         "--enable",  "DEBUG_INFO_NONE",
         "--disable", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT",
         "--disable", "DEBUG_INFO_DWARF4",
         "--disable", "DEBUG_INFO_DWARF5"])
    run(["make", "olddefconfig"])

    if args.no_build:
        print("--no-build set; stopping after setup.")
        return 0

    # 6. Build.
    threads = os.cpu_count() or 8
    if wsl:
        run(["make", f"-j{threads}", "KCONFIG_CONFIG=.config"])
    else:
        run(["make", f"-j{threads}", "deb-pkg"])

    if args.no_install:
        print("--no-install set; build complete, not installing.")
        return 0

    # 7. Install.
    if wsl:
        wsl_kernel_dir.mkdir(parents=True, exist_ok=True)
        kver = run(["make", "-s", "kernelrelease"], capture=True).stdout.strip()
        run(["cp", "arch/x86/boot/bzImage",
             str(wsl_kernel_dir / f"bzImage-{kver}")])
        # Update .wslconfig to point at new kernel.
        _update_wslconfig(wsl_config, wsl_kernel_dir / f"bzImage-{kver}",
                          args.win_user)
        print(f"WSL kernel installed. Restart WSL: wsl.exe --shutdown")
    else:
        # Ship debs to the remote and reboot.
        kver = run(["make", "-s", "kernelrelease"], capture=True).stdout.strip()
        debs = list(Path("..").glob(f"linux-image-{kver}*.deb"))
        if not debs:
            debs = list(Path(".").glob(f"../linux-image-{kver}*.deb"))
        if debs:
            run(["scp", *[str(d) for d in debs], f"{remote}:/tmp/"])
            _ssh(remote, "sudo dpkg -i /tmp/linux-image-*.deb && sudo reboot now")
        else:
            print("WARNING: no .deb found to ship; manual install required.")

    return 0


def _update_wslconfig(config_path: Path, kernel_path: Path, win_user: str) -> None:
    wsl_kpath = str(kernel_path).replace("/mnt/c/", "C:\\").replace("/", "\\")
    if config_path.exists():
        lines = config_path.read_text().splitlines()
        new_lines = []
        for line in lines:
            if line.strip().startswith("kernel="):
                new_lines.append(f"kernel={wsl_kpath}")
            else:
                new_lines.append(line)
        if not any(l.strip().startswith("kernel=") for l in lines):
            if not any("[wsl2]" in l for l in lines):
                new_lines.insert(0, "[wsl2]")
            new_lines.append(f"kernel={wsl_kpath}")
        config_path.write_text("\n".join(new_lines) + "\n")
    else:
        config_path.write_text(f"[wsl2]\nkernel={wsl_kpath}\n")
    print(f"Updated {config_path}")
