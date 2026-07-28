## KNOD Testing Guide

This document records what has been built and what is needed to continue
testing KNOD on a bare-metal node. It is intended to be self-contained so
work can be picked up on a different machine.

### Background

KNOD (in-kernel network offload device) is a 13-patch RFC series by Taehee
Yoo that enables GPU-accelerated packet processing (XDP offload and IPsec RX)
entirely within the Linux kernel. No userspace GPU runtime (ROCm, CUDA) is
involved in the data path. The NIC DMAs received packets directly into GPU
memory; the GPU runs BPF/XDP programs across a batch of packets in parallel;
only PASS-verdict packets are copied back to the host.

- Cover letter: https://lore.kernel.org/dri-devel/20260719175857.4071636-1-ap420073@gmail.com/
- Development tree: https://github.com/TaeheeYoo/knod/tree/knod-7.2
- LPC 2025 talk: https://lpc.events/event/19/contributions/2267/

### What has already been done (in kernel-tools)

Everything lives in the `sbates130272/kernel-tools` repository (release v1.0.0):

| Path | Purpose |
| --- | --- |
| `patches/knod/` | Full 13-patch series as a single mbox, fetched via `b4` |
| `scripts/build-latest-knod-kernel` | Wrapper around `build-kernel-debrpm` — clones the dev tree and builds a `.deb` |
| `userspace/knod-xdp-test/` | C test program that drives the KNOD control plane via generic netlink |

### Hardware requirements

WSL2 and virtual machines cannot be used — KNOD requires bare-metal PCIe access
for NIC-to-GPU direct DMA. The hypervisor layers in WSL2 (Hyper-V WDDM) and
standard VMs block the required P2P DMA path, prevent `amdkfd` from allocating
real GPU hardware queues, and provide no supported NIC.

**Required bare-metal node:**

| Component | Requirement |
| --- | --- |
| GPU | RDNA2 — RX 6600, 6700, 6800, 6900, 7xxx series, or Radeon PRO V620. **GCN/Vega will not work** — the SPSC ring requires 64-bit atomics that GCN does not implement. The kernel attach step will fail. |
| NIC | Mellanox ConnectX (`mlx5e`) or Broadcom (`bnxt_en`). These are the two NIC drivers patched in the series (patches 10/13 and 11/13). |
| PCIe topology | GPU and NIC ideally on the same root complex / PCIe switch for lowest-latency P2P DMA. |
| OS | Bare-metal Linux. No hypervisor, no container. Ubuntu 22.04 or 24.04 recommended. |
| Kernel | Custom build from the dev tree — see below. |

### Step 1: Clone kernel-tools on the test node

```sh
git clone https://github.com/sbates130272/kernel-tools.git
cd kernel-tools
```

### Step 2: Build the KNOD kernel

The easiest path clones the author's dev tree (patches already applied):

```sh
./scripts/build-latest-knod-kernel
```

This produces a `.deb` tarball in the current directory. By default it clones
`https://github.com/TaeheeYoo/knod` branch `knod-7.2` and builds with
`useful-configs/config-x86_64-p2pdma` as the base config.

**Before building**, ensure these options are enabled in your config:

```
CONFIG_NET_KNOD=y
CONFIG_AMD_KFD_KNOD=y
CONFIG_AMDGPU=y
CONFIG_HSA_AMD=y
CONFIG_NET_DEVMEM=y
CONFIG_BPF_JIT=y
CONFIG_XDP_SOCKETS=y
CONFIG_MLX5_CORE=m        # if using Mellanox NIC
CONFIG_BNXT=m             # if using Broadcom NIC
```

You can override the config:

```sh
CONFIG=./useful-configs/config-x86_64-p2pdma-tiny ./scripts/build-latest-knod-kernel
```

Or build from net-next with the mbox applied instead of the dev tree:

```sh
USE_DEV_TREE=no ./scripts/build-latest-knod-kernel
```

### Step 3: Install the kernel

```sh
tar xf build-kernel-deb.*.tar.gz
sudo dpkg -i linux-image-*.deb linux-headers-*.deb
sudo reboot
```

Confirm the right kernel booted:

```sh
uname -r
```

### Step 4: Verify KNOD is loaded

```sh
# knod generic netlink family should be present
ls /sys/bus/platform/drivers/knod 2>/dev/null || \
    grep knod /proc/net/protocols || \
    python3 -c "import socket; s=socket.socket(socket.AF_NETLINK,socket.SOCK_RAW,16); print('netlink ok')"

# amdkfd should have registered an accelerator
dmesg | grep -i knod
```

### Step 5: Build the userspace test

```sh
sudo apt install -y libbpf-dev libmnl-dev
make -C userspace/knod-xdp-test
```

### Step 6: Use the YNL CLI to inspect the system

The in-tree YNL CLI (from the knod kernel source tree) is the reference control
plane tool. From the kernel source directory:

```sh
SPEC=Documentation/netlink/specs/knod.yaml
CLI="tools/net/ynl/pyynl/cli.py --spec $SPEC"

# List accelerators (GPU devices visible to knod)
$CLI --dump accel-get

# List current NIC <-> accelerator bindings
$CLI --dump dev-get
```

### Step 7: Run the userspace test

```sh
# Replace eth0 with your mlx5e or bnxt_en interface name
sudo ./userspace/knod-xdp-test/knod_xdp_test -i eth0 -v
```

This will:
1. Resolve the `knod` generic netlink family
2. List available accelerators
3. Attach the NIC to accelerator 0
4. Enable BPF/XDP feature
5. Load an `XDP_PASS` BPF program via the GPU JIT
6. Send 16 raw test packets through the GPU dispatch loop
7. Detach and exit

A successful run confirms the GPU SPSC ring, JIT path, and verdict delivery
are all working.

### Step 8: Try a real XDP program

The in-tree selftests (patch 12/13) include two BPF programs:

```sh
# From the kernel source tree
cd tools/testing/selftests/drivers/net/knod

# Loopback test — XDP_TX, packets bounce back out the NIC from GPU memory
sudo ./knod_xdp_loop.sh --nic <ifname>

# Timestamp test — XDP program reads a GPU clock and writes it to the packet
sudo ./knod_xdp_ktime.sh --nic <ifname>
```

### Step 9: Performance measurement

The cover letter benchmarks used `kondor` (a katran-based L4 load balancer as
the offloaded XDP program). Reference numbers on RDNA2:

| GPU | Throughput | GPU power |
| --- | --- | --- |
| RX 6600 | 32 Mpps | 41 W |
| Radeon PRO V620 | 70 Mpps | 80 W |

### Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `knod genl family not found` | Kernel not built with `CONFIG_NET_KNOD=y`, or wrong kernel booted |
| `attach` fails immediately | GPU is GCN/Vega (missing 64-bit atomics) — must use RDNA2 |
| `attach` fails with `ENODEV` | NIC is not `mlx5e` or `bnxt_en` |
| BPF load fails with `EINVAL` | `CONFIG_BPF_JIT=y` not set, or `CONFIG_AMD_KFD_KNOD=y` missing |
| No accelerators listed | `amdkfd` not loaded — check `dmesg` for GPU enumeration errors |

### Refreshing the patches

If the series is updated upstream:

```sh
cd kernel-tools
b4 am 20260719175857.4071636-1-ap420073@gmail.com
mv 20260719_ap420073_net_knod_in_kernel_network_offload_device.mbx patches/knod/
mv 20260719_ap420073_net_knod_in_kernel_network_offload_device.cover patches/knod/
```
