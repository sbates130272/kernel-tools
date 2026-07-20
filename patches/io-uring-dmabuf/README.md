# io-uring-dmabuf patches

Pavel Begunkov's patchset adding DMA-BUF read/write support to io_uring,
enabling direct P2P transfers between NVMe storage and GPU VRAM without
staging through host RAM.

## Patchset source

Fetched from lore.kernel.org using b4:

```
b4 am cover.1777475843.git.asml.silence@gmail.com
```

To refresh or re-fetch the patches into this directory, run the above command
with `--no-apply`, rename the output files to the `NNNN-<slug>.patch`
convention, and commit them here.

## Patch application

These patches are applied by `scripts/build-latest-io-uring-dmabuf-kernel`
via the `PATCH=` variable passed to `scripts/build-kernel-debrpm`, which
applies all `*.patch` files in this directory using `patch -p1`.

The patches target a kernel around v6.14. Applying to a significantly
different kernel version may require manual conflict resolution.

## Kernel UAPI changes

Once the patches are applied, the following are added to
`include/uapi/linux/io_uring.h`:

- `IO_REGBUF_TYPE_DMABUF` — new buffer registration type
- `IORING_RSRC_UPDATE_EXTENDED` — flag enabling the extended descriptor path
- `struct io_uring_regbuf_desc` — extended buffer descriptor carrying
  `dmabuf_fd` and `target_fd`

The userspace test program in `userspace/amdgpu-dmabuf-io/` carries local
copies of these definitions and can be updated once the patched kernel headers
are installed.
