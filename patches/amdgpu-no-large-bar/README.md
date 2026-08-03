# amdgpu-no-large-bar patches

A two-patch series that adds "hipfile" support to the amdgpu kernel driver
for systems **without** large BAR (resizable BAR / ReBAR).

## Background

### Large BAR

GPUs normally expose two PCIe BAR regions for VRAM:
- A small fixed BAR (~256 MB) that is always present.
- A *large* BAR (also called resizable BAR or ReBAR) that maps the full VRAM
  into PCIe address space.

Without large BAR, only the first ~256 MB of VRAM is accessible to other PCIe
devices (e.g. NVMe controllers) via DMA.  Any BO allocated beyond that aperture
window cannot participate in peer-to-peer DMA.

The amdgpu driver tracks this via:
- `adev->gmc.real_vram_size` — total physical VRAM.
- `adev->gmc.visible_vram_size` — the aperture-accessible portion.

When `real_vram_size > visible_vram_size` the system lacks large BAR.

### hipfile

"hipfile" refers to the pattern of exporting a HIP GPU memory allocation as a
DMA-BUF file descriptor and then registering that file descriptor with
io_uring's DMA-BUF buffer registration interface (see `patches/io-uring-dmabuf/`
and the test program in `userspace/amdgpu-dmabuf-io/`).  This enables direct P2P
reads/writes between NVMe storage and GPU memory without staging through host RAM.

On large-BAR systems the GPU VRAM is directly addressable by NVMe's DMA engine
so true peer-to-peer (NVMe ↔ VRAM) is possible.  On systems without large BAR
the VRAM is not fully accessible and a fallback through system RAM (GTT) is
needed.

## Patches

### 0001 — amdgpu/dma-buf: fall back to GTT when large BAR is absent

**File changed:** `amd/amdgpu/amdgpu_dma_buf.c`

Adds `amdgpu_dma_buf_evict_to_gtt()`, a static helper that uses
`ttm_bo_validate()` (with the BO reservation lock already held) to move a
VRAM-resident BO into GTT.  The helper returns `-EBUSY` if the BO is pinned and
cannot be relocated.

In `amdgpu_dma_buf_map()`, the `TTM_PL_VRAM` branch now checks whether
`real_vram_size > visible_vram_size`.  If true, it calls the helper before
building the scatter-gather table so the importer receives system-RAM-backed DMA
addresses it can always reach via standard PCIe DMA.

The existing VRAM-direct P2P path on large-BAR systems is completely unchanged.

### 0002 — amdgpu/uapi,object: add `PREFER_GTT_NO_LARGE_BAR` GEM create flag

**Files changed:** `include/uapi/drm/amdgpu_drm.h`, `amd/amdgpu/amdgpu_object.c`

Introduces `AMDGPU_GEM_CREATE_PREFER_GTT_NO_LARGE_BAR` (bit 16) to let callers
declare upfront that a BO will be used as a DMA-BUF P2P target.  On no-large-BAR
systems `amdgpu_bo_placement_from_domain()` redirects VRAM allocations to GTT
before the placement-building loop runs.

This complements patch 1: the lazy eviction in `amdgpu_dma_buf_map()` handles
existing code that does not set the flag, while callers that set the flag get
immediate GTT placement without any eviction overhead at DMA-BUF map time.

## Applying

These patches are intended to be applied via `scripts/build-amdgpu-dkms`:

```bash
KERNEL_VER=6.8.0-136-generic \
  AMDGPU_REF=therock-7.14 \
  PATCH_DIRS=./patches/amdgpu-no-large-bar \
  ./scripts/build-amdgpu-dkms
```

Or combined with other patch series using colon-separated `PATCH_DIRS`:

```bash
KERNEL_VER=6.8.0-136-generic \
  AMDGPU_REF=therock-7.14 \
  PATCH_DIRS=./patches/amdgpu-no-large-bar \
  INSTALL=no \
  ./scripts/build-amdgpu-dkms
```

## Target source version

Patches target the **therock-7.14** AMD amdgpu tree (roughly kernel v6.14 era).
Applying to significantly different versions may require manual context
adjustment; the build script uses `patch --fuzz=3` which tolerates minor
differences.

Key context identifiers used by the patches:
- `amdgpu_dma_buf_map()` function signature and its `TTM_PL_VRAM` case.
- `amdgpu_bo_placement_from_domain()` declarations block and the first
  `if (domain & AMDGPU_GEM_DOMAIN_VRAM)` check.
- `AMDGPU_GEM_CREATE_GFX12_DCC` definition in `amdgpu_drm.h`.

## Testing

1. Verify large BAR status:

```bash
# No large BAR: visible_vram_size will be less than the total VRAM reported
# by lspci / glxinfo.
sudo cat /sys/bus/pci/devices/0000:XX:XX.0/resource
```

2. Build and install the patched DKMS module (see *Applying* above).

3. Run the io_uring DMA-BUF test (requires the io-uring-dmabuf kernel too):

```bash
cd userspace/amdgpu-dmabuf-io
make
sudo ./test_p2p_io /dev/nvme0n1
```

On a no-large-BAR system the kernel will log a `dev_warn_ratelimited` message
the first time a VRAM BO is evicted to GTT for a DMA-BUF map.  Subsequent maps
of the same BO (already in GTT) will not emit the warning.

## See also

- `patches/io-uring-dmabuf/` — the io_uring DMA-BUF patchset that this series
  complements.
- `patches/p2pdma/` — PCIe P2P DMA patches (ACS disable, p2pmem driver).
- `userspace/amdgpu-dmabuf-io/` — test program exercising the full hipfile path.
- `scripts/build-amdgpu-dkms` — DKMS build and patch-application script.
