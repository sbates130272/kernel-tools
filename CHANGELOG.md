# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.1.0] - 2026-10-22

### Added

- **`ktool build-cross`** — cross-compile a Linux kernel for ARM64 or RISC-V,
  porting `arch-tools/build-arm64` and `arch-tools/build-riscv`. Configurable
  cross-compiler prefix, module/header/output directories, and thread count.
- **`ktool build-kernel`** — clone a kernel branch and build deb/rpm packages,
  porting `scripts/build-kernel-debrpm`. Includes `--preset-dmabuf` (Anuj's
  fs-dmabuf-rfc-v11-desc-v2), `--preset-knod` (Taehee Yoo's KNOD tree), and
  `--preset-p2pdma` convenience shortcuts that set appropriate remotes and
  patch directories automatically.
- **`ktool build-amdgpu-dkms`** — stage and build the amdgpu DKMS module,
  porting `scripts/build-amdgpu-dkms`. Supports ccache injection, source .deb
  generation, pre-staged source directories, and patch application.
- **`ktool fast-build-amdgpu`** — incremental amdgpu OOT build that bypasses
  DKMS to preserve .o files between rebuilds, porting `scripts/fast-build-amdgpu`
  and `scripts/docker-fast-build-amdgpu`. Source hash check skips staging when
  the ref and patch set are unchanged.
- **`ktool build-remote`** — build a `localmodconfig` kernel for a remote host
  or local VM, porting `scripts/build-remote`. Supports WSL2 mode, config
  script fragments, and optional install/reboot.
- **`ktool init-update`** — initialise or update the kernel source git repo
  with standard remotes (linus, stable, wsl2, lk-library, amdgpu,
  ubuntu-noble), porting `scripts/init-update`.
- **`ktool dmabuf-test`** — full DMA-BUF P2P validation end-to-end: spin up a
  rocjitsu GPU VM, build and install the 7.2.x backport kernel, build the
  amdgpu-dkms module, run raw NVMe and XFS P2P tests. Python port of
  `run-this.py` (which itself was a port of `run-this.sh`). Parallel amdgpu
  source copy overlaps kernel build; ccache is used for incremental rebuilds;
  all stages are idempotent and skippable.
- **`ktool bnxt build`** and **`ktool bnxt install`** — build OOT Broadcom
  bnxt_en/bnxt_re drivers and install them into the running kernel's module
  tree, porting `arch-tools/bnxt-oot-build` and `arch-tools/bnxt-drv-fix`.
