# kernel-tools: Linux kernel development tools

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-linux-lightgrey.svg)](README.md)
[![PyPI](https://img.shields.io/pypi/v/ktool.svg)](https://pypi.org/p/ktool)
[![ktool](https://github.com/sbates130272/kernel-tools/actions/workflows/ktool-test.yml/badge.svg)](https://github.com/sbates130272/kernel-tools/actions/workflows/ktool-test.yml)
[![Spelling](https://github.com/sbates130272/kernel-tools/actions/workflows/spell-check-test.yml/badge.svg)](https://github.com/sbates130272/kernel-tools/actions/workflows/spell-check-test.yml)
[![Userspace Build](https://github.com/sbates130272/kernel-tools/actions/workflows/userspace-build-test.yml/badge.svg)](https://github.com/sbates130272/kernel-tools/actions/workflows/userspace-build-test.yml)
[![AMDGPU DKMS](https://github.com/sbates130272/kernel-tools/actions/workflows/build-amdgpu-dkms-test.yml/badge.svg)](https://github.com/sbates130272/kernel-tools/actions/workflows/build-amdgpu-dkms-test.yml)
[![AMDGPU DKMS Matrix](https://github.com/sbates130272/kernel-tools/actions/workflows/build-amdgpu-dkms-matrix-test.yml/badge.svg)](https://github.com/sbates130272/kernel-tools/actions/workflows/build-amdgpu-dkms-matrix-test.yml)
[![Fast Build AMDGPU](https://github.com/sbates130272/kernel-tools/actions/workflows/fast-build-amdgpu-test.yml/badge.svg)](https://github.com/sbates130272/kernel-tools/actions/workflows/fast-build-amdgpu-test.yml)
[![Docker Fast Build AMDGPU](https://github.com/sbates130272/kernel-tools/actions/workflows/docker-fast-build-amdgpu-test.yml/badge.svg)](https://github.com/sbates130272/kernel-tools/actions/workflows/docker-fast-build-amdgpu-test.yml)
[![Init Update](https://github.com/sbates130272/kernel-tools/actions/workflows/init-update-test.yml/badge.svg)](https://github.com/sbates130272/kernel-tools/actions/workflows/init-update-test.yml)
[![Release](https://github.com/sbates130272/kernel-tools/actions/workflows/kernel-tools-release.yml/badge.svg)](https://github.com/sbates130272/kernel-tools/actions/workflows/kernel-tools-release.yml)
[![DMA-BUF VM Test](https://github.com/sbates130272/kernel-tools/actions/workflows/dmabuf-vm-test.yml/badge.svg)](https://github.com/sbates130272/kernel-tools/actions/workflows/dmabuf-vm-test.yml)
[![KNOD](https://img.shields.io/badge/KNOD-RFC%20v1-orange.svg)](https://lore.kernel.org/dri-devel/20260719175857.4071636-1-ap420073@gmail.com/)
[![io_uring DMA-BUF](https://img.shields.io/badge/io__uring%20DMA--BUF-v11%20for--next-green.svg)](https://github.com/anuj7781/linux-kernel/tree/anuj/fs-dmabuf-rfc-v11-desc-v2)
[![amdgpu no-large-BAR](https://img.shields.io/badge/amdgpu-no--large--BAR-blue.svg)](patches/amdgpu-no-large-bar/README.md)

A collection of Linux kernel development tools, exposed as a single
installable Python CLI: `ktool`. Covers cross-architecture kernel builds,
DKMS module management, remote VM kernel installation, amdgpu staging, and
end-to-end DMA-BUF P2P validation.

## Installation

```bash
pipx install ktool
```

Or from a checkout:

```bash
pip install ./ktool
```

Run `ktool --help` to see all subcommands.  A man page is bundled with the
package; open it with:

```bash
ktool man
```

## Subcommands

| Subcommand | Description |
| --- | --- |
| `build-cross` | Cross-compile a Linux kernel for ARM64 or RISC-V |
| `build-kernel` | Clone a kernel branch and build deb/rpm packages |
| `build-amdgpu-dkms` | Stage and build the amdgpu DKMS module |
| `fast-build-amdgpu` | Incremental amdgpu OOT build with persistent `.o` files |
| `build-remote` | Build a `localmodconfig` kernel targeting a remote host or VM |
| `init-update` | Initialise or update the kernel source git repo with standard remotes |
| `stage-amdgpu` | Stage amdgpu DKMS source from a GitHub release tarball (no git needed) |
| `dmabuf-test` | Full DMA-BUF P2P validation: rocjitsu GPU VM, 7.2.x kernel, raw NVMe + XFS |
| `bnxt` | Build or install OOT Broadcom bnxt\_en/bnxt\_re drivers |
| `man` | Open the bundled ktool man page |

## Examples

### Initialise the kernel source tree

```bash
ktool init-update --kernel-dir ./src --depth 1 --skip-launchpad
```

### Build the io\_uring DMA-BUF kernel (Anuj's fs-dmabuf v11 tree)

```bash
ktool build-kernel --preset-dmabuf
```

### Stage amdgpu source from a GitHub tarball and build the DKMS module

```bash
ktool stage-amdgpu therock-7.14 /tmp/amdgpu-staged

sudo apt install dkms autoconf automake build-essential dwarves \
  linux-headers-$(uname -r)

ktool build-amdgpu-dkms \
  --kernel-ver $(uname -r) \
  --build-src /tmp/amdgpu-staged \
  --no-install
```

### Incremental amdgpu build with ccache (bypass DKMS)

```bash
ktool fast-build-amdgpu \
  --kernel-ver 6.8.0-136-generic \
  --build-src /tmp/amdgpu-staged \
  --build-out-base ./amdgpu-build-out \
  --no-install
```

Re-running after a single-file edit recompiles only that translation unit and
relinks; everything else hits ccache. Warm builds typically complete in under 5s.

### Build a localmodconfig kernel for a remote VM

```bash
# Call from the top of a kernel source tree checked out locally
ktool build-remote \
  --remote-name my-test-vm \
  --config-script default \
  --label -my-feature
```

### Run the full DMA-BUF P2P test (requires rocjitsu GPU VM infrastructure)

```bash
ktool dmabuf-test
```

### Cross-compile for ARM64

```bash
# Call from the top of a kernel source tree
ktool build-cross --arch arm64 --config /path/to/.config --threads 32
```

## Workspace layout

The conventional workspace alongside this repo:

- **`src/`** — Linux kernel source tree (initialise with `ktool init-update`)
- **`useful-configs/`** — kernel `.config` fragments for common targets
- **`config-scripts/`** — kernel configuration fragments applied by `build-remote`
- **`patches/`** — patch series (p2pdma, amdgpu-no-large-bar, dmabuf-72x-backport, knod)
- **`configs/`** — personal configs (not tracked)
- **`debs/`** — built `.deb` output (not tracked)

## amdgpu DKMS — fast incremental builds

`ktool fast-build-amdgpu` keeps a persistent build output directory across
runs so the kernel build system can reuse unchanged `.o` files. Combined with
`ccache`, warm builds after a single-line edit typically run 3–4× faster than
a full DKMS rebuild. Use `--docker` to run the build inside the
`amdgpu-dkms-build` container (built from `docker/Dockerfile.amdgpu-dkms`
via `docker/amdgpu-dkms.hcl`).

### Benchmark (roc-7.2.x, 6.8.0-142-generic, 32 cores, Docker container)

| Scenario | Wall time |
| --- | --- |
| Cold build (empty ccache + buildout volumes) | ~2m15s |
| Warm rebuild (identical source, hot ccache + .o) | **~4.6s** |
| Single-file incremental (one `.c` touched) | **~11s** |
| Patched build (`--force`, re-sync + patch apply) | ~2m20s |
| Same patches, second run | **~4.6s** |

## docker

`docker/Dockerfile.amdgpu-dkms` builds a kernel-header-baked image for
amdgpu OOT builds. Use the HCL bake file:

```bash
KERNEL_VER=6.8.0-142-generic \
  docker buildx bake --builder default --load -f docker/amdgpu-dkms.hcl
```

> **AMD corporate networks:** pass `--builder default` — the remote
> `docker-container` builder fails behind ZScaler.

## patches

Patch series live under `patches/`:

- **`p2pdma/`** — PCIe peer-to-peer DMA patches (`acs_disable`, `p2pmem-pci`)
- **`amdgpu-no-large-bar/`** — amdgpu no-large-BAR hipfile patches for G4an
  and similar systems without resizable BAR support
- **`dmabuf-72x-backport/`** — io\_uring DMA-BUF v11 backport onto v7.2.x stable
- **`knod/`** — Taehee Yoo's KNOD in-kernel network offload RFC patches

## Release process

Cut a release from `main` with a clean working tree:

```bash
scripts/release.sh 0.2.0
```

This stamps the version in `ktool/pyproject.toml` and `CHANGELOG.md`,
commits with `-s`, and creates a signed GPG tag. Push to trigger the
release workflow:

```bash
git push origin main v0.2.0
```

The workflow verifies version alignment, builds the wheel and sdist, publishes
a GitHub release, then publishes to PyPI via Trusted Publishing (no API token
in the repo).
