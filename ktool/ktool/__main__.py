"""ktool entry point."""

import argparse
import sys

from ktool import __version__
from ktool import build_cross, build_kernel, build_amdgpu_dkms, fast_build_amdgpu
from ktool import build_remote, init_update, dmabuf_test, bnxt, stage_amdgpu, man


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ktool",
        description="Kernel development tools",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    build_cross.register(sub)
    build_kernel.register(sub)
    build_amdgpu_dkms.register(sub)
    fast_build_amdgpu.register(sub)
    build_remote.register(sub)
    init_update.register(sub)
    dmabuf_test.register(sub)
    bnxt.register(sub)
    stage_amdgpu.register(sub)
    man.register(sub)

    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass

    args = parser.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
