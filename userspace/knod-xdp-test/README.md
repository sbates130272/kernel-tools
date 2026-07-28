## knod-xdp-test

Userspace test for the KNOD in-kernel network offload device.

Exercises the full attach → feature-enable → XDP offload → traffic → detach
cycle via the `knod` generic netlink control plane. The BPF program loaded is
a minimal `XDP_PASS` that bounces every packet through the GPU dispatch loop
and back to the host stack — sufficient to verify that the GPU SPSC ring,
JIT path, and verdict delivery are all working.

### Dependencies

- Kernel with `CONFIG_NET_KNOD=y` and `CONFIG_AMD_KFD_KNOD=y`
- RDNA2 AMD GPU (GCN/Vega lacks the 64-bit atomics KNOD requires)
- Supported NIC: mlx5e (Mellanox ConnectX) or bnxt_en (Broadcom)
- libbpf >= 1.0 (`apt install libbpf-dev` / `dnf install libbpf-devel`)
- libmnl >= 1.0 (`apt install libmnl-dev` / `dnf install libmnl-devel`)

### Build

```sh
make -C userspace/knod-xdp-test
```

### Usage

```sh
sudo ./knod_xdp_test -i <ifname> [-a <accel-id>] [-n <pkt-count>] [-v]
```

Options:
- `-i <ifname>`   NIC to offload, e.g. `eth0` or `enp1s0`
- `-a <accel-id>` Accelerator id to use (default: 0; list with `accel-get`)
- `-n <count>`    Number of test packets to send (default: 16)
- `-v`            Verbose output

### What it does

1. Resolves the `knod` generic netlink family id
2. Dumps available accelerators (`accel-get`)
3. Attaches the NIC to accelerator 0 (`attach`)
4. Enables BPF/XDP offload feature on the accelerator (`accel-set`)
5. Loads an `XDP_PASS` BPF program with `BPF_F_XDP_DEV_BOUND_ONLY` (routes
   through KNOD's GPU JIT rather than the CPU JIT)
6. Attaches it to the NIC in `XDP_MODE_HW` via `RTM_SETLINK`
7. Sends `n` raw UDP packets and waits for the GPU verdict ring to drain
8. Detaches and exits

### Extending

To test a more interesting XDP program (e.g. a packet counter or L4 LB),
replace `xdp_pass_insns[]` with a compiled BPF object loaded via
`bpf_prog_load()` from libbpf, keeping `prog_ifindex` and
`BPF_F_XDP_DEV_BOUND_ONLY` set.
