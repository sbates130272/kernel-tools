# amdgpu-7.3-compat patches

Four-patch series that fixes the amdgpu DKMS kcl build failures when
building ref `amdgpu/roc-7.1.x` against Linux 7.3.  A larger structural
breakage (drm_atomic_state rename) is identified and documented below but
is not fully addressed by these patches.

## Background

The amdgpu DKMS module uses an autoconf-based compatibility layer (kcl)
to adapt to API changes across kernel versions.  Feature detection happens
at `pre-build.sh` time via `configure`, which runs small compile tests and
writes `config/config.h`.  Guard macros like `HAVE_DRM_FB_HELPER_FILL_INFO`
then suppress or enable kcl shim code at compile time.

## Breakages and fixes

### 1 — drm_fb_helper_fill_info detection failure

**Files:** `amd/dkms/m4/drm-fb-helper-fill-info.m4`

The autoconf probe for `drm_fb_helper_fill_info()` includes `<drm/drmP.h>`,
which was removed from mainline before v7.3.  The compile test therefore
fails unconditionally on modern kernels, leaving `HAVE_DRM_FB_HELPER_FILL_INFO`
undefined.

With the flag absent, `kcl_drm_fb.c` tries to implement
`drm_fb_helper_fill_info()` itself by calling `drm_fb_helper_fill_fix()`
and `drm_fb_helper_fill_var()`.  Both of these functions became `static`
helpers inside `drm_fb_helper.c` in v7.x and are not accessible from
out-of-tree code, causing an undefined-symbol link error.

**Fix (patch 0001):** Remove the superfluous `#include <drm/drmP.h>` from
the compile test.  `drm_fb_helper.h` is self-contained; the probe succeeds
in v7.3 and `HAVE_DRM_FB_HELPER_FILL_INFO` is correctly set, bypassing the
broken kcl shim.

### 2 — zone_device_page_init signature change (3-arg in v7.x)

**Files:** `amd/dkms/m4/zone_device_page_init.m4`, `include/kcl/kcl_mm.h`,
`amd/amdkfd/kfd_migrate.c`

`zone_device_page_init()` changed from a 1-argument function in v6.x to a
3-argument form in v7.x:

```c
/* v6.x */
void zone_device_page_init(struct page *page);

/* v7.x */
void zone_device_page_init(struct page *page,
                           struct dev_pagemap *pgmap,
                           unsigned int order);
```

The existing autoconf probe calls `zone_device_page_init(NULL)` (1-arg).
This compile fails on v7.3, so `HAVE_ZONE_DEVICE_PAGE_INIT` is not set.
`kcl_mm.h` then re-declares the function with the old 1-arg prototype.
When `kfd_migrate.c` includes `<linux/hmm.h>` -> `<linux/mm.h>` ->
`<linux/memremap.h>`, it picks up the kernel's 3-arg declaration, creating
a "conflicting types" compile error.

**Fix (patches 0002-0004):**
- 0002: Extend the m4 probe to also try the 3-arg form and set a new
  flag `HAVE_ZONE_DEVICE_PAGE_INIT_3ARGS` when found.
- 0003: Add an inline `kcl_zone_device_page_init(page)` wrapper to
  `kcl_mm.h` that calls the right form: `zone_device_page_init(page,
  page_pgmap(page), 0)` when the 3-arg version is present.
- 0004: Replace the direct call in `kfd_migrate.c` with the kcl wrapper.

### 3 — drm_atomic_state -> drm_atomic_commit rename (KNOWN REMAINING ISSUE)

In Linux v7.3, `struct drm_atomic_state` was renamed to `struct drm_atomic_commit`
throughout the DRM subsystem.  The rename also added new fields to the struct
(`checked`, `plane_color_pipeline`, `colorops`) before the existing fields,
changing field offsets.

The amdgpu DKMS module carries a shadow `include/drm/drm_atomic.h` with the
old `struct drm_atomic_state` definition.  At compile time this shadow
header takes precedence so type-checking succeeds.  At module load time,
several non-inline lifecycle symbols are unresolved:
- `drm_atomic_state_alloc` (renamed to `drm_atomic_commit_alloc`)
- `__drm_atomic_state_free` (renamed to `__drm_atomic_commit_free`)

A simple pointer-cast shim is NOT safe because the struct layouts differ
(new fields were inserted before existing fields, shifting all offsets).

**This requires a full port of amdgpu_dm to use the new `drm_atomic_commit`
type.** The in-tree v7.3 amdgpu driver has already been updated.  Backporting
this port to the out-of-tree roc-7.1.x tree is a significant multi-file
change that is beyond the scope of this patch series.

## Patches

| File | What it fixes |
|------|---------------|
| 0001 | Remove drmP.h from fill_info autoconf probe |
| 0002 | Add 3-arg zone_device_page_init detection |
| 0003 | Add kcl_zone_device_page_init() wrapper |
| 0004 | Use wrapper in kfd_migrate.c |

## Applying

```bash
KERNEL_VER=7.3.0-rc2-dmabuf-v11-fs-g42c577f6e97f \
  AMDGPU_REF=roc-7.1.x \
  PATCH_DIRS=./patches/amdgpu-7.3-compat \
  INSTALL=no \
  ./scripts/build-amdgpu-dkms
```

Or with the no-large-BAR series chained:

```bash
KERNEL_VER=7.3.0-rc2-dmabuf-v11-fs-g42c577f6e97f \
  AMDGPU_REF=roc-7.1.x \
  PATCH_DIRS=./patches/amdgpu-7.3-compat:./patches/amdgpu-no-large-bar \
  INSTALL=no \
  ./scripts/build-amdgpu-dkms
```

## Target source version

Patches target **amdgpu/roc-7.1.x** (short SHA `01cee31f91e`).

All patches use `patch --fuzz=3` compatible context.  The zone_device probe
hunk applies with fuzz 2 due to a trailing space in the original m4 file.

## API changes covered

| Kernel API | Change in v7.x | Disposition |
|---|---|---|
| `drm_fb_helper_fill_fix()` | Made `static` in drm_fb_helper.c | Fixed — patch 0001 fixes probe |
| `drm_fb_helper_fill_var()` | Made `static` in drm_fb_helper.c | Fixed — same as above |
| `zone_device_page_init()` | Added `pgmap` + `order` args | Fixed — patches 0002-0004 |
| `drm_atomic_state_alloc()` | Renamed to `drm_atomic_commit_alloc()` | NOT FIXED — requires full amdgpu_dm port |
| `__drm_atomic_state_free()` | Renamed to `__drm_atomic_commit_free()` | NOT FIXED — same |
