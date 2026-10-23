// bench_p2p_io.cpp — DMA-BUF P2P latency/IOPS/BW benchmark
//
// For each block size: 1000 sequential reads + 1000 random reads from an
// XFS file into GPU VRAM via io_uring fixed DMA-BUF buffers.
// Reports mean latency (µs), p99 latency (µs), IOPS, bandwidth (MB/s).
//
// Usage: sudo ./bench_p2p_io <xfs-file>
//   e.g. sudo ./bench_p2p_io /mnt/nvme/test.bin

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <numeric>
#include <random>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <vector>

#include <amdgpu.h>
#include <amdgpu_drm.h>
#include <hip/hip_runtime.h>
#include <hsa/hsa.h>
#include <hsa/hsa_ext_amd.h>
#include <liburing.h>
#include <xf86drm.h>

// ---- UAPI from io_uring DMA-BUF patchset (same as test_p2p_io.cpp) ----
enum io_uring_regbuf_type {
    IO_REGBUF_TYPE_EMPTY  = 0,
    IO_REGBUF_TYPE_UADDR  = 1,
    IO_REGBUF_TYPE_DMABUF = 2,
};
struct io_uring_regbuf_desc {
    __u32 type;
    __u32 flags;
    __u64 size;
    __u64 uaddr;
    __s32 dmabuf_fd;
    __s32 target_fd;
    __u64 __resv[6];
};
#define IORING_RSRC_UPDATE_EXTENDED (1U << 1)
struct io_uring_rsrc_update2_dmabuf {
    __u32 offset;
    __u32 flags;
    __u64 data;
    __u64 tags;
    __u32 nr;
    __u32 resv;
};
// ---- end UAPI ----

static int64_t now_ns() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static double p99(std::vector<double> &v) {
    if (v.empty()) return 0;
    std::sort(v.begin(), v.end());
    size_t idx = (size_t)(0.99 * (v.size() - 1));
    return v[idx];
}

static double mean(const std::vector<double> &v) {
    if (v.empty()) return 0;
    return std::accumulate(v.begin(), v.end(), 0.0) / v.size();
}

struct BenchResult {
    size_t block_size;
    double seq_mean_us, seq_p99_us, seq_bw_mbs;
    double rnd_mean_us, rnd_p99_us, rnd_iops;
};

static int setup_ring(struct io_uring *ring, int dmabuf_fd, int target_fd) {
    if (io_uring_queue_init(256, ring, 0) < 0) { perror("io_uring_queue_init"); return -1; }
    if (io_uring_register_buffers_sparse(ring, 1) < 0) {
        perror("io_uring_register_buffers_sparse"); io_uring_queue_exit(ring); return -1;
    }
    struct io_uring_regbuf_desc desc = {
        .type = IO_REGBUF_TYPE_DMABUF, .flags = 0, .size = 0,
        .uaddr = 0, .dmabuf_fd = dmabuf_fd, .target_fd = target_fd,
    };
    struct io_uring_rsrc_update2_dmabuf up = {
        .offset = 0, .flags = IORING_RSRC_UPDATE_EXTENDED,
        .data = reinterpret_cast<__u64>(&desc), .tags = 0, .nr = 1, .resv = 0,
    };
    int ret = syscall(__NR_io_uring_register, ring->ring_fd,
                      IORING_REGISTER_BUFFERS_UPDATE, &up, sizeof(up));
    if (ret < 0) { perror("register dmabuf"); io_uring_queue_exit(ring); return -1; }
    return 0;
}

static double do_one_read(struct io_uring *ring, int target_fd,
                          size_t block_size, off_t offset) {
    int64_t t0 = now_ns();
    struct io_uring_sqe *sqe = io_uring_get_sqe(ring);
    io_uring_prep_read_fixed(sqe, target_fd, nullptr, block_size, offset, 0);
    io_uring_submit(ring);
    struct io_uring_cqe *cqe;
    io_uring_wait_cqe(ring, &cqe);
    int res = cqe->res;
    io_uring_cqe_seen(ring, cqe);
    int64_t t1 = now_ns();
    if (res < 0) return -1.0;
    return (t1 - t0) / 1000.0; // µs
}

static BenchResult bench_block_size(size_t block_size, int target_fd,
                                    int dmabuf_fd, off_t file_size,
                                    int iters) {
    // Each block size needs its own buffer registration (size must match)
    // We allocate a fresh GPU buffer sized to block_size.
    void *gpu_ptr = nullptr;
    uint64_t offset_out = 0;
    hipMalloc(&gpu_ptr, block_size);
    hsa_init();
    int bfd = -1;
    hsa_amd_portable_export_dmabuf(gpu_ptr, block_size, &bfd, &offset_out);

    struct io_uring ring;
    if (setup_ring(&ring, bfd, target_fd) < 0) {
        if (gpu_ptr) hipFree(gpu_ptr);
        if (bfd >= 0) close(bfd);
        return {block_size, 0, 0, 0, 0, 0, 0};
    }

    // Sequential reads
    std::vector<double> seq_lat;
    seq_lat.reserve(iters);
    for (int i = 0; i < iters; ++i) {
        off_t off = ((off_t)i * block_size) % (file_size - block_size);
        off = (off / block_size) * block_size; // align
        double us = do_one_read(&ring, target_fd, block_size, off);
        if (us >= 0) seq_lat.push_back(us);
    }

    // Random reads
    std::mt19937_64 rng(42);
    off_t max_off = (file_size - block_size) / block_size;
    std::vector<double> rnd_lat;
    rnd_lat.reserve(iters);
    for (int i = 0; i < iters; ++i) {
        off_t off = (rng() % (uint64_t)max_off) * block_size;
        double us = do_one_read(&ring, target_fd, block_size, off);
        if (us >= 0) rnd_lat.push_back(us);
    }

    io_uring_queue_exit(&ring);
    if (gpu_ptr) hipFree(gpu_ptr);
    if (bfd >= 0) close(bfd);

    double seq_mean = mean(seq_lat);
    double rnd_mean = mean(rnd_lat);

    BenchResult r;
    r.block_size  = block_size;
    r.seq_mean_us = seq_mean;
    r.seq_p99_us  = p99(seq_lat);
    r.seq_bw_mbs  = seq_mean > 0 ? (block_size / 1e6) / (seq_mean / 1e6) : 0;
    r.rnd_mean_us = rnd_mean;
    r.rnd_p99_us  = p99(rnd_lat);
    r.rnd_iops    = rnd_mean > 0 ? 1e6 / rnd_mean : 0;
    return r;
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <xfs-file>\n";
        return 1;
    }
    const char *fname = argv[1];

    int target_fd = open(fname, O_RDONLY | O_DIRECT);
    if (target_fd < 0) { perror("open"); return 1; }

    struct stat st;
    if (fstat(target_fd, &st) < 0 || st.st_size == 0) {
        std::cerr << "Cannot stat file or empty\n"; close(target_fd); return 1;
    }
    off_t file_size = st.st_size;
    std::cerr << "File size: " << file_size / (1<<20) << " MiB\n";

    static const size_t block_sizes[] = {
        4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288
    };
    static const int N = sizeof(block_sizes) / sizeof(block_sizes[0]);
    const int ITERS = 1000;

    std::vector<BenchResult> results;
    for (int i = 0; i < N; ++i) {
        size_t bs = block_sizes[i];
        if ((off_t)bs > file_size) continue;
        std::cerr << "Benchmarking " << bs/1024 << " KB...\n";
        results.push_back(bench_block_size(bs, target_fd, -1, file_size, ITERS));
    }
    close(target_fd);

    // CSV
    std::cout << "\n--- CSV ---\n";
    std::cout << "block_size_kb,seq_mean_us,seq_p99_us,seq_bw_mbs,"
                 "rnd_mean_us,rnd_p99_us,rnd_iops\n";
    for (auto &r : results) {
        std::cout << r.block_size/1024 << ","
                  << (int)r.seq_mean_us << "," << (int)r.seq_p99_us << ","
                  << (int)r.seq_bw_mbs << ","
                  << (int)r.rnd_mean_us << "," << (int)r.rnd_p99_us << ","
                  << (int)r.rnd_iops << "\n";
    }

    // Markdown tables
    std::cout << "\n--- Markdown ---\n";
    std::cout << "### Sequential reads (NVMe XFS → AMD VRAM via io_uring DMA-BUF)\n\n";
    std::cout << "| Block size | BW (MB/s) | Mean lat (µs) | p99 lat (µs) |\n";
    std::cout << "|---|---|---|---|\n";
    for (auto &r : results)
        std::cout << "| " << r.block_size/1024 << " KB | "
                  << (int)r.seq_bw_mbs << " | "
                  << (int)r.seq_mean_us << " | "
                  << (int)r.seq_p99_us << " |\n";

    std::cout << "\n### Random reads (NVMe XFS → AMD VRAM via io_uring DMA-BUF)\n\n";
    std::cout << "| Block size | IOPS | Mean lat (µs) | p99 lat (µs) |\n";
    std::cout << "|---|---|---|---|\n";
    for (auto &r : results)
        std::cout << "| " << r.block_size/1024 << " KB | "
                  << (int)r.rnd_iops << " | "
                  << (int)r.rnd_mean_us << " | "
                  << (int)r.rnd_p99_us << " |\n";

    return 0;
}
