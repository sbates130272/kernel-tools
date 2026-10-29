"""build-cross — cross-compile a Linux kernel for ARM64 or RISC-V.

Ports arch-tools/build-arm64 and arch-tools/build-riscv.
Must be called from the top-level of a kernel source tree.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from ktool._util import run, die, timestamp

_ARCH_DEFAULTS = {
    "arm64": {
        "cross": "aarch64-linux-gnu-",
        "image": "arch/arm64/boot/Image",
    },
    "riscv": {
        "cross": "riscv64-linux-gnu-",
        "image": "arch/riscv/boot/Image",
    },
}


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("build-cross",
                        help="Cross-compile kernel for ARM64 or RISC-V")
    p.add_argument("--arch", choices=["arm64", "riscv"], default="arm64",
                   help="Target architecture (default: arm64)")
    p.add_argument("--config", default=".config",
                   help="Kernel .config to use (default: .config)")
    p.add_argument("--cross-tools",
                   help="Cross-compiler prefix (e.g. aarch64-linux-gnu-)")
    p.add_argument("--module-dir",
                   help="Where to install modules (default: ../modules-<arch>)")
    p.add_argument("--header-dir",
                   help="Where to install headers (default: ../headers-<arch>)")
    p.add_argument("--output-dir",
                   help="Where to place output artefacts (default: ../output-<arch>)")
    p.add_argument("--target-host", default="none",
                   help="Remote host to scp kernel/modules to (not yet implemented)")
    p.add_argument("--clean", action="store_true",
                   help="Run 'make clean' before building")
    p.add_argument("--threads", type=int, default=16,
                   help="Parallel build jobs (default: 16)")
    p.set_defaults(func=run_cmd)


def run_cmd(args: argparse.Namespace) -> int:
    defaults = _ARCH_DEFAULTS[args.arch]
    cross = args.cross_tools or defaults["cross"]
    image_path = defaults["image"]

    module_dir = Path(args.module_dir or f"../modules-{args.arch}")
    header_dir = Path(args.header_dir or f"../headers-{args.arch}")
    output_base = Path(args.output_dir or f"../output-{args.arch}")
    time_tag = timestamp()
    output_dir = Path(f"{output_base}-{time_tag}")

    config = Path(args.config)
    if not config.exists():
        die(f"Config file not found: {config}")

    env = {"ARCH": args.arch, "CROSS_COMPILE": cross,
           **dict(os.environ)}

    shutil.rmtree(module_dir, ignore_errors=True)

    if args.clean:
        run(["make", "clean"], env=env)

    shutil.copy(config, ".config")
    run(["make", "oldconfig"], env=env)

    import shutil as _sh
    _sh.copy(".config", f"../.config-{time_tag}")

    run(["make", f"-j{args.threads}"], env=env)
    run(["make", f"INSTALL_MOD_PATH={module_dir}", "modules_install"], env=env)
    run(["make", f"INSTALL_HDR_PATH={header_dir}", "headers_install"], env=env)

    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(".config", output_dir / f".config-{time_tag}")
    shutil.copy(image_path, output_dir / f"Image-{time_tag}")

    mods_tar = module_dir / "lib" / "modules" / "modules.tar.gz"
    run(["tar", "cvfz", "modules.tar.gz", "--exclude=modules.tar.gz", "."],
        cwd=module_dir / "lib" / "modules")
    shutil.copy(mods_tar, output_dir / f"modules-{time_tag}.tar.gz")

    run(["tar", "cvfz", "headers.tar.gz", str(header_dir)])
    shutil.copy("headers.tar.gz", output_dir / f"headers-{time_tag}.tar.gz")
    shutil.rmtree(module_dir, ignore_errors=True)
    shutil.rmtree(header_dir, ignore_errors=True)

    print(f"Artefacts in {output_dir}")
    if args.target_host != "none":
        print("NOTE: --target-host scp is not yet implemented.")
    return 0
