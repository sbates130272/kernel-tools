# DMA-BUF IO Patchset Backport to Linux 7.2.4

Backport of the 17-commit DMA-BUF io_uring patchset from
`anuj/fs-dmabuf-rfc-v11-desc-v2` (based on Linux 7.3.0-rc2) to the
`v7.2.4` stable kernel tag.

## Source

- Upstream: <https://github.com/anuj7781/linux-kernel>
  branch `anuj/fs-dmabuf-rfc-v11-desc-v2`
- Base: `v7.2.4` stable tag from `git.kernel.org`

## Patches

Patches 0001-0017 are the 17 upstream commits, applied via `patch --fuzz=5`.
Patches 0018-0020 are backport fixups for 7.2.4 API differences.

## Conflicts and Resolutions

### Patch 0001 — `include/linux/fs.h` (fuzz 1)

The patch context expects `struct io_uring_cmd;` and `struct offset_ctx;` to
be adjacent; in 7.2.4 a `typedef unsigned int __bitwise fop_flags_t;` was
inserted between them. Applied with `--fuzz=1`.

The hunk adding `int (*mmap_prepare)` applied with offset 2 (new `fop_flags`
member shifted the line count).

### Patch 0004 — `block/bio.c` and `block/fops.c` (manual resolution)

**Root cause:** `bio_iov_bvec_set()` was renamed to `bio_iov_iter_set()`
(changing return type from `void` to `bool`) between 7.2.4 and 7.3. The
patch was written against the 7.3 base where the rename had already occurred.

**bio.c hunk 2** (failed): The patch expected `bio_iov_iter_set` to exist
but found `bio_iov_bvec_set`. Manually replaced the old function with the
new `bio_iov_iter_set` that returns bool and handles both bvec and dmabuf_map
iterator types.

**fops.c hunk 1** (failed): The reject was a comment-only change in
`__blkdev_direct_IO_async`. The surrounding code in 7.2.4 uses
`if (iov_iter_is_bvec(iter)) { bio_iov_bvec_set(...) } else {...}`. The
anuj branch replaced this with `if (!bio_iov_iter_set(bio, iter)) {...}`.
Applied manually: removed the outer `iov_iter_is_bvec()` guard and replaced
`bio_iov_bvec_set()` with `bio_iov_iter_set()` returning a bool.

**bio.c — `bio_iov_iter_get_pages`**: Updated to call `bio_iov_iter_set()`
and use it in a conditional (returns bool) in place of the old
`iov_iter_is_bvec()` guard + `bio_iov_bvec_set()` call.

**`include/linux/bio.h`**: Updated the declaration from
`void bio_iov_bvec_set(...)` to `bool bio_iov_iter_set(...)`.

**`block/blk-map.c`**: The `blk_rq_map_user_bvec()` function still called
`bio_iov_bvec_set()`; updated to `bio_iov_iter_set()`.

### Patch 0013 — `io_uring/rw.c` (fuzz placement error)

`io_detach_dmabuf_map(req)` was placed outside the closing brace of
`io_complete_rw()` due to a fuzz mismatch. Manually moved it inside the
function body, matching the anuj branch layout.

### Fixup 0018 — `bi_offset` → `bi_bvec_done` (7.2.4 `bvec_iter` compat)

In Linux 7.3, `struct bvec_iter.bi_bvec_done` was renamed to `bi_offset`.
The anuj branch was written against 7.3, so patches 3 and 4 use `bi_offset`.
In 7.2.4 the field is still `bi_bvec_done`. Fixed in:

- `include/linux/bio.h` — `bio_advance_iter` and `bio_advance_iter_single`
- `block/bio.c` — `bio_iov_iter_set`
- `block/blk-merge.c` — `dmabuf_map_get_sgl_size`
- `drivers/nvme/host/pci.c` — three sites in dmabuf PRP/SGL helpers

### Fixup 0019 — `blk-map.c`, `dm-io-rewind.c`, `io_uring/rw.c`

Additional `bi_bvec_done` fixes and the `io_detach_dmabuf_map` placement fix
from patch 0013.

### Fixup 0020 — `IO_BUF_DEST` / `IO_BUF_SOURCE` constants

In the anuj branch (7.3 base) the direction flags were renamed:
- `IO_IMU_DEST` → `IO_BUF_DEST` (moved to `include/linux/io_uring_types.h`)
- `IO_IMU_SOURCE` → `IO_BUF_SOURCE`

In 7.2.4 only `IO_IMU_DEST` / `IO_IMU_SOURCE` exist, defined in
`io_uring/rsrc.h`. The new `io_uring/dma-buf.c` (patch 0013) uses the new
names. Fixed by substituting `IO_IMU_DEST | IO_IMU_SOURCE` in `dma-buf.c`.

## Build

```bash
cd /tmp/linux-72x   # 7.2.4 tree with patches applied
make defconfig
scripts/config -m DRM_AMDGPU -m XFS_FS --disable DEBUG_INFO \
  --set-str LOCALVERSION -dmabuf-72x
make olddefconfig
make -j$(nproc) deb-pkg KDEB_PKGVERSION=$(make kernelversion)-1
```

Output debs land in `/tmp/`:
- `linux-image-7.2.4-dmabuf-72x-<sha>_7.2.4-1_amd64.deb`
- `linux-headers-7.2.4-dmabuf-72x-<sha>_7.2.4-1_amd64.deb`

## Verification

After booting the patched kernel, DMA-BUF io symbols are present:

```
$ sudo grep -E 'dma_buf_io|iomap_file_init_dma_buf|bdev_init_dma_buf' /proc/kallsyms | head
ffffffff... T iomap_file_init_dma_buf_io_ctx
ffffffff... T bdev_init_dma_buf_io_ctx
ffffffff... t blkdev_init_dma_buf_io_ctx
```

## amdgpu DKMS Status

amdgpu DKMS `roc-7.2.x` has unresolved API incompatibilities with the
7.2.4 kernel (KCL layer mismatches: `migrate_disable` static decl conflict,
`dma_fence.lock` removal, `shmem_file_setup` signature, `drm_client_dev_*`
arg count). These require additional KCL patches beyond scope. `/dev/kfd`
is therefore absent from the current VM state.
