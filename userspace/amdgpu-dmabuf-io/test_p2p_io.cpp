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
#include <hsa/hsa.h>
#include <hsa/hsa_ext_amd.h>

// ---- UAPI definitions from Pavel Begunkov's io_uring DMA-BUF patchset v11 ----
//
// Merged into Jens Axboe's for-next tree (git://git.kernel.dk/linux).
// Added to include/uapi/linux/io_uring.h by patches 10 and 13 of the series.
// Once a kernel from that tree (or a release containing it) is installed,
// these local definitions can be removed.

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

// Flag added to io_uring_rsrc_update2::flags by patch 10.
#define IORING_RSRC_UPDATE_EXTENDED (1U << 1)

// io_uring_rsrc_update2 with the flags field added by the patchset.
// The system liburing version has 'resv2' here; we define our own to
// avoid depending on a patched liburing being installed.
struct io_uring_rsrc_update2_dmabuf {
    __u32 offset;
    __u32 flags;   // IORING_RSRC_UPDATE_EXTENDED when set
    __u64 data;
    __u64 tags;
    __u32 nr;
    __u32 resv;
};

// ---- end UAPI definitions ----

// Allocate VRAM via hipMalloc and export as DMA-BUF using the HSA portable
// export API (hsa_amd_portable_export_dmabuf). This is the preferred path
// as it uses the HIP runtime allocation directly.
//
// gpu_ptr_out receives the hipMalloc'd pointer (caller must hipFree).
// offset_out receives the byte offset into the dmabuf where gpu_ptr resides.
// Returns a valid dmabuf fd on success, -1 on failure.
static int hip_alloc_dmabuf(size_t size, void **gpu_ptr_out, uint64_t *offset_out)
{
    void *gpu_ptr = nullptr;
    hipError_t herr = hipMalloc(&gpu_ptr, size);
    if (herr != hipSuccess) {
        std::cerr << "hipMalloc: " << hipGetErrorString(herr) << "\n";
        return -1;
    }

    // Ensure HSA runtime is initialised (HIP initialises it, but call explicitly)
    hsa_init();

    int dmabuf_fd = -1;
    uint64_t offset = 0;
    hsa_status_t status = hsa_amd_portable_export_dmabuf(gpu_ptr, size,
                                                          &dmabuf_fd, &offset);
    if (status != HSA_STATUS_SUCCESS) {
        const char *msg = nullptr;
        hsa_status_string(status, &msg);
        std::cerr << "hsa_amd_portable_export_dmabuf failed: "
                  << (msg ? msg : "unknown") << " (" << status << ")\n";
        hipFree(gpu_ptr);
        return -1;
    }

    *gpu_ptr_out  = gpu_ptr;
    *offset_out   = offset;
    return dmabuf_fd;
}

// Fallback: allocate VRAM directly via libdrm amdgpu_bo_alloc when the
// hipMalloc path is unavailable.
static int bo_alloc_dmabuf(size_t size,
                            amdgpu_device_handle *adev_out,
                            amdgpu_bo_handle *bo_out)
{
    int device_idx = 0;
    hipGetDevice(&device_idx);

    char drm_path[32];
    snprintf(drm_path, sizeof(drm_path), "/dev/dri/renderD%d", 128 + device_idx);

    int drm_fd = open(drm_path, O_RDWR);
    if (drm_fd < 0) { perror("open DRM render node"); return -1; }

    amdgpu_device_handle adev;
    uint32_t major, minor;
    if (amdgpu_device_initialize(drm_fd, &major, &minor, &adev) != 0) {
        std::cerr << "amdgpu_device_initialize failed\n";
        close(drm_fd); return -1;
    }
    close(drm_fd);

    struct amdgpu_bo_alloc_request req = {};
    req.alloc_size     = size;
    req.phys_alignment = 4096;
    req.preferred_heap = AMDGPU_GEM_DOMAIN_VRAM;
    req.flags          = AMDGPU_GEM_CREATE_NO_CPU_ACCESS;

    amdgpu_bo_handle bo;
    if (amdgpu_bo_alloc(adev, &req, &bo) != 0) {
        std::cerr << "amdgpu_bo_alloc failed\n";
        amdgpu_device_deinitialize(adev); return -1;
    }

    uint32_t shared_handle = 0;
    if (amdgpu_bo_export(bo, amdgpu_bo_handle_type_dma_buf_fd, &shared_handle) != 0) {
        std::cerr << "amdgpu_bo_export failed\n";
        amdgpu_bo_free(bo); amdgpu_device_deinitialize(adev); return -1;
    }

    *adev_out = adev;
    *bo_out   = bo;
    return static_cast<int>(shared_handle);
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

    // Try hipMalloc + hsa_amd_portable_export_dmabuf first (preferred path).
    // Fall back to amdgpu_bo_alloc if HSA export is unavailable.
    void *gpu_ptr = nullptr;
    uint64_t dmabuf_offset = 0;
    amdgpu_device_handle adev = nullptr;
    amdgpu_bo_handle bo = nullptr;
    int dmabuf_fd = -1;

    std::cout << "[+] Allocating " << (size >> 20) << " MiB of GPU VRAM via hipMalloc\n";
    dmabuf_fd = hip_alloc_dmabuf(size, &gpu_ptr, &dmabuf_offset);
    if (dmabuf_fd >= 0) {
        std::cout << "[+] Exported via hsa_amd_portable_export_dmabuf: fd="
                  << dmabuf_fd << " offset=" << dmabuf_offset << "\n";
    } else {
        std::cout << "[~] hipMalloc path failed, falling back to amdgpu_bo_alloc\n";
        dmabuf_fd = bo_alloc_dmabuf(size, &adev, &bo);
        if (dmabuf_fd < 0) {
            std::cerr << "Both allocation paths failed.\n";
            close(target_fd);
            return 1;
        }
        std::cout << "[+] Allocated via amdgpu_bo_alloc: fd=" << dmabuf_fd << "\n";
    }

    std::cout << "[+] Initialising io_uring (256 entries)\n";
    struct io_uring ring;
    if (io_uring_queue_init(256, &ring, 0) < 0) {
        perror("io_uring_queue_init");
        close(dmabuf_fd);
        if (bo) { amdgpu_bo_free(bo); amdgpu_device_deinitialize(adev); } if (gpu_ptr) hipFree(gpu_ptr);
        close(target_fd);
        return 1;
    }

    // First, set up a one-slot buffer table.
    std::cout << "[+] Registering buffer table (1 slot)\n";
    if (io_uring_register_buffers_sparse(&ring, 1) < 0) {
        perror("io_uring_register_buffers_sparse");
        io_uring_queue_exit(&ring);
        close(dmabuf_fd);
        if (bo) { amdgpu_bo_free(bo); amdgpu_device_deinitialize(adev); } if (gpu_ptr) hipFree(gpu_ptr);
        close(target_fd);
        return 1;
    }

    // Fill slot 0 with our DMA-BUF using the extended descriptor path.
    // This uses IORING_REGISTER_BUFFERS_UPDATE with IORING_RSRC_UPDATE_EXTENDED.
    std::cout << "[+] Registering DMA-BUF as fixed buffer (slot 0)\n";
    struct io_uring_regbuf_desc desc = {
        .type      = IO_REGBUF_TYPE_DMABUF,
        .flags     = 0,
        .size      = 0,    // must be 0 for IO_REGBUF_TYPE_DMABUF
        .uaddr     = 0,
        .dmabuf_fd = dmabuf_fd,
        .target_fd = target_fd,
    };
    struct io_uring_rsrc_update2_dmabuf up = {
        .offset = 0,
        .flags  = IORING_RSRC_UPDATE_EXTENDED,
        .data   = reinterpret_cast<__u64>(&desc),
        .tags   = 0,
        .nr     = 1,
        .resv   = 0,
    };
    // Always use the raw syscall — the system liburing's struct predates
    // the flags field so io_uring_register_buffers_update_tag won't work.
    int ret = -1;
    if (ret < 0) {
        // Fall back to raw syscall so we can pass the extended flags.
        // nr_args for IORING_REGISTER_BUFFERS_UPDATE is sizeof(up), not the count.
        // The count of entries is inside up.nr.
        ret = syscall(__NR_io_uring_register, ring.ring_fd,
                      IORING_REGISTER_BUFFERS_UPDATE, &up, sizeof(up));
    }
    if (ret < 0) {
        perror("register dmabuf buffer");
        std::cerr << "Is the patched kernel running?\n";
        io_uring_queue_exit(&ring);
        close(dmabuf_fd);
        if (bo) { amdgpu_bo_free(bo); amdgpu_device_deinitialize(adev); } if (gpu_ptr) hipFree(gpu_ptr);
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
    if (bo) { amdgpu_bo_free(bo); amdgpu_device_deinitialize(adev); } if (gpu_ptr) hipFree(gpu_ptr);
    close(target_fd);
    return ret;
}
