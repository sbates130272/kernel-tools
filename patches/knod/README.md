## KNOD patches

Patches for the KNOD (in-kernel network offload device) RFC series by Taehee
Yoo, which enables GPU-accelerated packet processing (XDP offload and IPsec
RX) entirely from within the Linux kernel — no userspace GPU runtime required.

Posted to dri-devel and netdev on 2026-07-19, targeting net-next.

Cover letter: https://lore.kernel.org/dri-devel/20260719175857.4071636-1-ap420073@gmail.com/

Development tree: https://github.com/TaeheeYoo/knod/tree/knod-7.2

### Patches in this series (13 patches)

1. net: knod: add uapi and core headers
2. net: devmem: extend memory provider for knod
3. net: core: add XDP_MODE_HW offload hook for knod
4. net: knod: add offload device core and control plane
5. bpf: offload: allow PERCPU_ARRAY maps for offloaded programs
6. drm/amdkfd: prepare kfd core for the knod provider
7. drm/amdkfd: add knod provider core
8. drm/amdkfd: add GPU instruction emitter and disassembler
9. drm/amdkfd: add BPF-to-GPU JIT offload
10. net/mlx5e: add knod XDP offload support
11. bnxt_en: add knod XDP offload support
12. selftests: drivers/net: add knod tests
13. drm/amdkfd: add IPsec full-packet offload

### Refreshing patches

```
b4 am 20260719175857.4071636-1-ap420073@gmail.com
mv 20260719_ap420073_net_knod_in_kernel_network_offload_device.mbx patches/knod/
mv 20260719_ap420073_net_knod_in_kernel_network_offload_device.cover patches/knod/
```

### Hardware

Reference GPUs: GCN (Radeon RX Vega64) and RDNA2 (Radeon RX 6600, Radeon PRO
V620). RDNA2 is the primary target — GCN lacks the 64-bit atomics the data
path requires.

### NIC support

mlx5e (Mellanox ConnectX) and bnxt_en (Broadcom) in this series.
