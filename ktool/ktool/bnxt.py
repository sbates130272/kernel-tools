"""bnxt — build and install OOT bnxt_en/bnxt_re drivers.

Ports arch-tools/bnxt-oot-build and arch-tools/bnxt-drv-fix.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from ktool._util import run, die


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("bnxt",
                        help="Build or install OOT Broadcom bnxt_en/bnxt_re drivers")
    sp = p.add_subparsers(dest="bnxt_cmd", metavar="<subcommand>")
    sp.required = True

    build = sp.add_parser("build", help="Build OOT bnxt_en and bnxt_re drivers")
    build.add_argument("--dir", default="./",
                       help="Path to directory containing bnxt_en/ and bnxt_re/")
    build.add_argument("--kdir", default="/home/users/sbates/kernel/linux-arm64",
                       help="Kernel source/build directory")
    build.add_argument("--clean", action=argparse.BooleanOptionalAction, default=True,
                       help="Run 'make clean' before building (default: yes)")
    build.add_argument("--verbose", action="store_true",
                       help="Print ARCH and CROSS_COMPILE before building")
    build.set_defaults(func=_build)

    install = sp.add_parser("install",
                             help="Install pre-built bnxt_en.ko and bnxt_re.ko")
    install.add_argument("--bnxt-en", default="bnxt_en.ko",
                         help="Path to bnxt_en.ko (default: ./bnxt_en.ko)")
    install.add_argument("--bnxt-re", default="bnxt_re.ko",
                         help="Path to bnxt_re.ko (default: ./bnxt_re.ko)")
    install.add_argument("--kernel", default="",
                         help="Kernel version (default: current uname -r)")
    install.set_defaults(func=_install)

    p.set_defaults(func=lambda a: p.print_help() or 1)


def _build(args: argparse.Namespace) -> int:
    src_dir = Path(args.dir)
    kdir = Path(args.kdir)
    here = Path.cwd()

    env = dict(os.environ)
    if args.verbose:
        print(f"ARCH={env.get('ARCH', '')}  CROSS_COMPILE={env.get('CROSS_COMPILE', '')}")

    en_dir = src_dir / "bnxt_en"
    if not en_dir.exists():
        die(f"bnxt_en source not found at {en_dir}")
    if args.clean:
        run(["make", "clean"], cwd=en_dir, env=env)
    run(["make", f"LINUXSRC={kdir}"], cwd=en_dir, env=env)
    shutil.copy(en_dir / "bnxt_en.ko", here / "bnxt_en.ko")

    re_dir = src_dir / "bnxt_re"
    if not re_dir.exists():
        die(f"bnxt_re source not found at {re_dir}")
    if args.clean:
        run(["make", "clean"], cwd=re_dir, env=env)
    run(["make", f"LINUXSRC={kdir}", f"LINUX={kdir}",
         f"OFA_KERNEL_PATH={kdir}"], cwd=re_dir, env=env)
    shutil.copy(re_dir / "bnxt_re.ko", here / "bnxt_re.ko")

    print("Built bnxt_en.ko and bnxt_re.ko")
    return 0


def _install(args: argparse.Namespace) -> int:
    import subprocess
    kernel = args.kernel or subprocess.check_output(
        ["uname", "-r"], text=True).strip()

    en_ko = Path(args.bnxt_en)
    re_ko = Path(args.bnxt_re)
    if not en_ko.exists():
        die(f"bnxt_en.ko not found: {en_ko}")
    if not re_ko.exists():
        die(f"bnxt_re.ko not found: {re_ko}")

    en_dst = Path(f"/lib/modules/{kernel}/kernel/drivers/net/ethernet/broadcom/bnxt")
    re_dst = Path(f"/lib/modules/{kernel}/kernel/drivers/infiniband/hw/bnxt_re")

    run(["mkdir", "-p", str(en_dst), str(re_dst)], sudo=True)
    run(["cp", str(en_ko), str(en_dst / "bnxt_en.ko")], sudo=True)
    run(["cp", str(re_ko), str(re_dst / "bnxt_re.ko")], sudo=True)
    run(["depmod"], sudo=True)
    print(f"bnxt_en and bnxt_re installed for kernel {kernel}")
    return 0
