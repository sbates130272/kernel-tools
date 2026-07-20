#include <iostream>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <liburing.h>
#include <xf86drm.h>
#include <amdgpu.h>
#include <amdgpu_drm.h>
#include <hip/hip_runtime.h>

// ---- UAPI definitions from Pavel Begunkov's io_uring DMA-BUF patchset v3 ----
//
// These are added to include/uapi/linux/io_uring.h by patches 9 and 10.
// Once the patched kernel is installed these will be in the system headers
// and these local definitions can be removed.

enum io_uring_regbuf_type {
    IO_REGBUF_TYPE_EMPTY  = 0,
    IO_REGBUF_TYPE_UADDR  = 1,
    IO_REGBUF_TYPE_DMABUF = 2,
};

// Passed as the extended descriptor when IORING_RSRC_UPDATE_EXTENDED is set.
struct io_uring_regbuf_desc {
    __u32 type;       // enum io_uring_regbuf_type
    __u32 flags;
    __u64 size;
    __u64 uaddr;      // unused for IO_REGBUF_TYPE_DMABUF
    __s32 dmabuf_fd;  // DMA-BUF fd wrapping the GPU allocation
    __s32 target_fd;  // file the buffer is registered against (e.g. NVMe fd)
    __u64 __resv[6];
};

// Flag added to io_uring_rsrc_update2::flags by patch 9.
#define IORING_RSRC_UPDATE_EXTENDED (1U << 1)

// ---- end UAPI definitions ----

// Export a HIP device pointer as a DMA-BUF file descriptor via the amdgpu
// DRM PRIME path. The HIP runtime does not expose this directly; we reach
// into the DRM device to find the GEM handle backing the allocation.
//
// This requires the process to have access to /dev/dri/renderDN and the
// allocation to have been made with hipMalloc (device memory, not managed).
//
// Returns a valid dmabuf fd on success, -1 on failure.
static int hip_ptr_to_dmabuf(void *gpu_ptr, size_t size)
{
    int device_idx = 0;
    hipGetDevice(&device_idx);

    // HIP device 0 maps to /dev/dri/renderD128 on most systems.
    char drm_path[32];
    snprintf(drm_path, sizeof(drm_path), "/dev/dri/renderD%d", 128 + device_idx);

    int drm_fd = open(drm_path, O_RDWR);
    if (drm_fd < 0) {
        perror("open DRM render node");
        return -1;
    }

    amdgpu_device_handle adev;
    uint32_t major, minor;
    if (amdgpu_device_initialize(drm_fd, &major, &minor, &adev) != 0) {
        std::cerr << "amdgpu_device_initialize failed\n";
        close(drm_fd);
        return -1;
    }

    // Query the GEM handle for this VA via the amdgpu DRM VA ioctl.
    // If this path doesn't work on your driver version, use amdgpu_bo_alloc
    // directly (bypassing HIP) and call amdgpu_bo_export instead.
    struct drm_amdgpu_gem_va va_query = {};
    va_query.va_address = reinterpret_cast<uint64_t>(gpu_ptr);
    va_query.operation  = AMDGPU_VA_OP_CLEAR;
    if (ioctl(drm_fd, DRM_IOCTL_AMDGPU_GEM_VA, &va_query) != 0) {
        std::cerr << "GEM VA query failed; use amdgpu_bo_alloc path instead.\n";
        amdgpu_device_deinitialize(adev);
        close(drm_fd);
        return -1;
    }

    int dmabuf_fd = -1;
    if (drmPrimeHandleToFD(drm_fd, va_query.handle, DRM_CLOEXEC | DRM_RDWR,
                           &dmabuf_fd) != 0) {
        perror("drmPrimeHandleToFD");
        amdgpu_device_deinitialize(adev);
        close(drm_fd);
        return -1;
    }

    amdgpu_device_deinitialize(adev);
    close(drm_fd);
    return dmabuf_fd;
}

int main(int argc, char *argv[])
{
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <nvme-device-or-file>\n"
                  << "  The target must support O_DIRECT (e.g. /dev/nvme0n1).\n";
        return 1;
    }
    const char *filename = argv[1];
    const size_t size = 4 * 1024 * 1024; // 4 MiB

    std::cout << "[+] Opening storage target with O_DIRECT: " << filename << "\n";
    int target_fd = open(filename, O_RDONLY | O_DIRECT);
    if (target_fd < 0) {
        perror("open storage target");
        return 1;
    }

    std::cout << "[+] Allocating " << (size >> 20) << " MiB of GPU VRAM via HIP\n";
    void *gpu_ptr = nullptr;
    hipError_t err = hipMalloc(&gpu_ptr, size);
    if (err != hipSuccess) {
        std::cerr << "hipMalloc: " << hipGetErrorString(err) << "\n";
        close(target_fd);
        return 1;
    }

    std::cout << "[+] Exporting VRAM allocation as DMA-BUF\n";
    int dmabuf_fd = hip_ptr_to_dmabuf(gpu_ptr, size);
    if (dmabuf_fd < 0) {
        std::cerr << "Failed to obtain DMA-BUF fd for GPU allocation.\n";
        hipFree(gpu_ptr);
        close(target_fd);
        return 1;
    }

    std::cout << "[+] Initialising io_uring (256 entries)\n";
    struct io_uring ring;
    if (io_uring_queue_init(256, &ring, 0) < 0) {
        perror("io_uring_queue_init");
        close(dmabuf_fd);
        hipFree(gpu_ptr);
        close(target_fd);
        return 1;
    }

    // First, set up a one-slot buffer table.
    std::cout << "[+] Registering buffer table (1 slot)\n";
    if (io_uring_register_buffers_sparse(&ring, 1) < 0) {
        perror("io_uring_register_buffers_sparse");
        io_uring_queue_exit(&ring);
        close(dmabuf_fd);
        hipFree(gpu_ptr);
        close(target_fd);
        return 1;
    }

    // Fill slot 0 with our DMA-BUF using the extended descriptor path.
    // This uses IORING_REGISTER_BUFFERS_UPDATE with IORING_RSRC_UPDATE_EXTENDED.
    std::cout << "[+] Registering DMA-BUF as fixed buffer (slot 0)\n";
    struct io_uring_regbuf_desc desc = {
        .type      = IO_REGBUF_TYPE_DMABUF,
        .flags     = 0,
        .size      = size,
        .uaddr     = 0,
        .dmabuf_fd = dmabuf_fd,
        .target_fd = target_fd,
    };
    struct io_uring_rsrc_update2 up = {
        .offset = 0,          // slot index
        .flags  = IORING_RSRC_UPDATE_EXTENDED,
        .data   = reinterpret_cast<__u64>(&desc),
        .tags   = 0,
        .nr     = 1,
    };
    int ret = io_uring_register_buffers_update_tag(&ring, 0,
                  reinterpret_cast<struct iovec *>(&up), nullptr, 1);
    if (ret < 0) {
        // Fall back to raw syscall so we can pass the extended flags.
        ret = syscall(__NR_io_uring_register, ring.ring_fd,
                      IORING_REGISTER_BUFFERS_UPDATE, &up, 1);
    }
    if (ret < 0) {
        perror("register dmabuf buffer");
        std::cerr << "Is the patched kernel running?\n";
        io_uring_queue_exit(&ring);
        close(dmabuf_fd);
        hipFree(gpu_ptr);
        close(target_fd);
        return 1;
    }

    // Issue a fixed read: kernel drives NVMe DMA directly into GPU VRAM.
    // The request must use the same target_fd specified at registration time.
    std::cout << "[+] Submitting fixed read into GPU VRAM (slot 0)\n";
    struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
    io_uring_prep_read_fixed(sqe, target_fd, nullptr, size, /*offset=*/0, /*buf_index=*/0);
    io_uring_submit(&ring);

    struct io_uring_cqe *cqe;
    io_uring_wait_cqe(&ring, &cqe);

    if (cqe->res == -EAGAIN) {
        // The patchset documents that spurious EAGAIN is possible; reissue.
        std::cout << "[~] EAGAIN — reissuing\n";
        io_uring_cqe_seen(&ring, cqe);
        sqe = io_uring_get_sqe(&ring);
        io_uring_prep_read_fixed(sqe, target_fd, nullptr, size, 0, 0);
        io_uring_submit(&ring);
        io_uring_wait_cqe(&ring, &cqe);
    }

    if (cqe->res < 0) {
        std::cerr << "[-] P2P read failed: " << strerror(-cqe->res) << "\n";
        ret = 1;
    } else {
        std::cout << "[+] Success: " << cqe->res
                  << " bytes transferred directly into AMD VRAM\n";
        ret = 0;
    }

    io_uring_cqe_seen(&ring, cqe);
    io_uring_queue_exit(&ring);
    close(dmabuf_fd);
    hipFree(gpu_ptr);
    close(target_fd);
    return ret;
}
