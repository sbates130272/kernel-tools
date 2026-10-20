# amdgpu-72x-compat patches

Compatibility patches for building `roc-7.2.x` amdgpu DKMS against a Linux
7.2.x kernel (specifically `7.2.4-dmabuf-72x-*` with the DMA-BUF P2P backport
patches applied).

## Usage

```bash
KERNEL_VER=7.2.4-dmabuf-72x-00020-g5e429f861097 \
  AMDGPU_REF=amdgpu/roc-7.2.x \
  PATCH_DIRS=./patches/amdgpu-72x-compat \
  ./scripts/build-amdgpu-dkms
```

## Patch series

### 0001 — Fix ccache CC quoting in dkms/Makefile
`amd/dkms/Makefile` passes `$(CC)` to `pre-build.sh` unquoted; when CC is
`ccache gcc` (a two-word string), the shell splits it and configure receives
only `ccache` as the compiler, causing a configure failure.  Quote `$(CC)` so
that the multi-word string is passed as a single argument.

### 0002 — Remove dma_map_ops.map_resource ops member (7.2+)
`struct dma_map_ops.map_resource` was removed in 7.2+.  `dma_map_resource()`
is still available as a top-level API, so `kcl_has_dma_map_resource_ops()`
returns `true` unconditionally.

### 0003 — Fix dma_fence_array_create arg count (7.2+)
`dma_fence_array_create()` had its `irq_on_signaling` bool argument removed in
7.2+.  Drop the trailing `false` from the call in `kcl_dma-resv.c`.

### 0004 — Fix migrate_disable static-inline conflict (7.2+)
In 7.2+, `migrate_disable()` is declared in `linux/sched.h` as `extern void`
for modules.  The kcl wrapper in `kcl_preempt.h` also defines it as
`static __always_inline`, causing a redeclaration conflict.  Guard the kcl
definition with `LINUX_VERSION_CODE < 7.2`.

### 0005 — Fix dma_fence.lock removal in amdgpu_vm.h (7.2+)
`struct dma_fence.lock` (a `spinlock_t *`) was removed in 7.2+ and replaced by
`extern_lock`/`inline_lock` accessors.  Use `dma_fence_spinlock()` (newly
exported) in the `amdgpu_vm_tlb_seq()` inline, guarded by version.  Also adds
`#include <linux/dma-fence.h>` and `#include <linux/version.h>`.

### 0006 — Fix drm_client_dev_suspend/resume 1-arg API (7.2+)
In 7.2+, `drm_client_dev_suspend()` and `drm_client_dev_resume()` dropped the
`bool holds_console_lock` argument.  The kcl header now provides static-inline
2→1-arg wrappers on 7.2+, and the `.c` implementation (which called
`drm_fb_helper_set_suspend_unlocked`) is guarded to older kernels only.

### 0007 — Guard linux/pagevec.h include (7.2+)
`linux/pagevec.h` was removed in 7.2+ (pagevec replaced by folio_batch).
`kcl_page_alloc.c` included it but never used any pagevec types.  Guard the
include with `LINUX_VERSION_CODE < 7.2`.

### 0008 — Add dma_buf_move_notify alias (7.2+)
`dma_buf_move_notify()` was renamed to `dma_buf_invalidate_mappings()` in 7.2+.
Add a static-inline compatibility wrapper in `kcl_dma-buf.h` for 7.2+ kernels.

### 0009 — Guard drm_fb_helper_fill_info fallback (7.2+)
The autoconf test for `HAVE_DRM_FB_HELPER_FILL_INFO` includes `drm/drmP.h`
which was removed in 7.2+, causing the test to fail even though the function
exists.  Guard the kcl fallback implementation to kernels < 7.2 using a version
check, allowing the kernel-provided implementation to be used on 7.2+.

### 0010 — Wrap pci_resize_resource 4-arg API (7.2+)
`pci_resize_resource()` gained a 4th `exclude_bars` argument in 7.2+.  Add a
3-arg macro wrapper in `kcl_pci.h` that passes `0` for `exclude_bars`.

### 0011 — Fix shmem_file_setup vma_flags_t arg type (7.2+)
`shmem_file_setup()` changed its third argument type from `unsigned long` to
`vma_flags_t` (a struct) in 7.2+.  Pass `EMPTY_VMA_FLAGS` on 7.2+ in
`ttm_backup.c` and `ttm_tt.c`.

### 0012 — Fix ccache CC quoting in pre-build.sh
`pre-build.sh` passes `CC` to `./configure` as `CC=${CC}` (unquoted), which
splits the two-word `ccache gcc` string into separate shell words so configure
only sees `CC=ccache`.  Change to `CC="${CC}"` to pass the whole string as one
`KEY=VALUE` argument.

### 0013 — Fix dma_fence.lock accesses in amdgpu_ring/vm/dma_buf (7.2+)
Three more sites directly access the removed `fence->lock` field:
- `amdgpu_ring.c`: use `dma_fence_lock_irqsave/unlock_irqrestore` macros (7.2+)
- `amdgpu_vm.c`: same
- `amdgpu_dma_buf.c`: rename `dma_buf_attach_ops.move_notify` →
  `.invalidate_mappings` (new name in 7.2+)

### 0014 — Fix bin_attrs const and DCE mode_set_base_atomic (7.2+)
- `amdgpu_ras.c`, `amdgpu_psp.c`: `attribute_group.bin_attrs` changed to
  `const struct bin_attribute *const *` in 7.2+.  Guard the `const` declaration
  with a version check.
- `dce_v6_0.c`, `dce_v8_0.c`, `dce_v10_0.c`: `drm_crtc_helper_funcs.
  mode_set_base_atomic` and `enum mode_set_atomic` were removed in 7.2+.
  Guard the dead code with `LINUX_VERSION_CODE < 7.2`.

### 0015 — Fix amdgpu_vkms atomic commit API (7.2+)
`drm_crtc_helper_funcs.atomic_{flush,enable,disable}` and
`drm_plane_helper_funcs.atomic_{check,update}` changed their second parameter
from `drm_crtc/connector_state *` to `drm_atomic_commit *` in 7.2+.  Add 7.2+
branches to the existing `#if HAVE_...` chains.

### 0016 — Fix zap_vma_ptes and dma_fence_signal (7.2+)
- `kcl_mm.h`: `zap_vma_ptes()` was removed in 7.2+.  Provide a static-inline
  replacement that calls `zap_special_vma_range()` (exported) for VM_PFNMAP
  VMAs (doorbell use case).
- `kfd_process.c`: `dma_fence_signal()` changed return type to `void` in 7.2+.
  Guard the return-value usage with a version check.

### 0017 — Undefine CONFIG_DRM_AMD_DC in backport.h (7.2+)
The amdgpu_dm display stack is not ported to the 7.2+ DRM API (the
`drm_atomic_state` → `drm_atomic_commit` rename affects dozens of files
throughout the display manager, trace infrastructure, and MST helpers).
Rather than port the entire display stack (weeks of work, not needed for ROCm
compute), undefine `CONFIG_DRM_AMD_DC` and `CONFIG_DRM_AMD_DC_FP` at the end
of `backport.h` (which is force-included after `linux/kconfig.h` that sets
them) so that `#if CONFIG_DRM_AMD_DC` guards in `amdgpu_debugfs.c`,
`amdgpu_discovery.c`, and similar files don't reference the uncompiled display
symbols.

### 0018 — Skip display Makefile inclusion on 7.2+ (amdgpu/Makefile)
Prevent the `amd/display/Makefile` (and the `AMD_DISPLAY_FILES` object list)
from being included in the build on 7.2+ kernels.  Also adds a
`-include kcl_no_display_72x.h` flag so the `#undef` takes effect for files
compiled from `amd/amdgpu/` that the Kbuild `subdir-ccflags-y` doesn't reach.

### 0019 — Skip display in Kbuild on 7.2+ (amd/dkms/Kbuild)
Mirror the version guard in the DKMS Kbuild so that `subdir-ccflags-y` also
carries `-UCONFIG_DRM_AMD_DC` for subdirectories, and the `CONFIG_DRM_AMD_DC_FP`
export is skipped on 7.2+.

### 0020 — Add kcl_no_display_72x.h
New file `amd/dkms/kcl_no_display_72x.h`: a minimal force-include header that
`#undef`s `CONFIG_DRM_AMD_DC` and `CONFIG_DRM_AMD_DC_FP`.  Applied via
`-include` in `amd/amdgpu/Makefile` on 7.2+ kernels to override the
`autoconf.h` definitions after `linux/kconfig.h` has been processed.

## Limitations

- The **amdgpu_dm display stack is disabled** on 7.2+ kernels.  All
  `/dev/dri/renderD*` nodes and DRM modesetting are unavailable.  The KFD
  (`/dev/kfd`) and compute path remain functional.
- The `drm_atomic_state` → `drm_atomic_commit` API rename in 7.2+ DRM requires
  a full port of `amdgpu_dm`, which is a significant engineering effort.
- GPU probe requires the `amdgpu/ip_discovery.bin` firmware to be present
  (loaded from VRAM in rocjitsu emulation with `discovery=2`).  This firmware
  is provided by the rocjitsu VFIO-user server; the probe will fail without it.
