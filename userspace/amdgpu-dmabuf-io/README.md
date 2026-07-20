# amdgpu-dmabuf-io

A userspace test program for P2P direct I/O from NVMe storage into AMD GPU VRAM
using io_uring's DMA-BUF buffer registration, bypassing host RAM entirely.

This requires a kernel built with Pavel Begunkov's io_uring DMA-BUF patchset
(see `patches/io-uring-dmabuf/` and `scripts/build-latest-io-uring-dmabuf-kernel`).

## Prerequisites

- Kernel built from `scripts/build-latest-io-uring-dmabuf-kernel` installed and running
- ROCm / HIP toolkit (provides `hipcc` and `hip/hip_runtime.h`)
- `liburing-dev` (`sudo apt install liburing-dev`)
- `libdrm-dev` and `libdrm-amdgpu1` (`sudo apt install libdrm-dev`)
- NVMe device that supports `O_DIRECT` and P2PDMA (PCIe peer-to-peer)
- AMD GPU supported by the amdgpu kernel driver

## Build

```sh
cd userspace/amdgpu-dmabuf-io
make
```

Override the compiler or flags if needed:

```sh
CXX=/opt/rocm/bin/hipcc CXXFLAGS="-O3 -g" make
```

## Run

```sh
sudo ./test_p2p_io /dev/nvme0n1
```

The target device must support `O_DIRECT`. On success the program reports the
number of bytes transferred directly into GPU VRAM.

## Notes

- `IORING_REGISTER_BUFFERS_DMABUF` (hardcoded as 32) must match the value in
  `include/uapi/linux/io_uring.h` of the patched kernel. Update the `#define`
  in `test_p2p_io.cpp` if it differs.
- The GEM handle retrieval in `hip_ptr_to_dmabuf()` uses an ioctl path that
  may not be available on all kernel/driver combinations. See the comment in
  that function for the `amdgpu_bo_alloc`-based alternative that bypasses HIP.
- P2PDMA requires that the GPU and NVMe device are on the same PCIe root
  complex, or that ACS is disabled (see `patches/p2pdma/` for the acs_disable
  patch used with the p2pdma patchset).
