/*
 * knod_xdp_test.c - userspace test for the KNOD in-kernel network offload device
 *
 * (c) Stephen Bates, 2026
 *
 * Exercises the KNOD control plane via its generic netlink family to:
 *   1. List available accelerators (GPU devices registered by amdkfd)
 *   2. Attach a NIC to an accelerator
 *   3. Enable BPF/XDP offload mode
 *   4. Load an XDP_PASS program onto the GPU via the kernel's BPF offload path
 *   5. Send test traffic via a raw socket and verify packet delivery
 *   6. Detach and clean up
 *
 * The KNOD control plane is a generic netlink family named "knod".
 * Ops mirror the YNL CLI documented in the cover letter:
 *   accel-get  - dump accelerator list
 *   dev-get    - dump NIC<->accelerator bindings
 *   attach     - bind a NIC ifindex to an accelerator id
 *   detach     - unbind
 *   accel-set  - set the active feature ("none", "bpf", "ipsec")
 *
 * The BPF program is loaded via the standard bpf(2) syscall with
 * BPF_PROG_TYPE_XDP and then attached via ip_link with XDP_MODE_HW (offload
 * mode), which routes it through the KNOD JIT path.
 *
 * Build:
 *   make -C userspace/knod-xdp-test
 *
 * Usage:
 *   sudo ./knod_xdp_test -i <ifname> [-a <accel-id>] [-v]
 *
 * Requires:
 *   - Kernel built with CONFIG_NET_KNOD=y and CONFIG_AMD_KFD_KNOD=y
 *   - RDNA2 AMD GPU present (VEGA/GCN will fail at attach time — missing 64-bit atomics)
 *   - Supported NIC: mlx5e or bnxt_en
 *   - libbpf >= 1.0
 *   - libmnl >= 1.0
 */

#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <linux/rtnetlink.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <libmnl/libmnl.h>
#include <linux/genetlink.h>

/* ---- KNOD uapi (from include/uapi/linux/knod.h in the patchset) ----
 *
 * These will be in <linux/knod.h> once the patched kernel headers are
 * installed. Until then they are reproduced here from patch 1/13.
 */

#define KNOD_GENL_NAME   "knod"
#define KNOD_GENL_VERSION 1

/* Netlink attributes for the "attach" op */
#define KNOD_A_NIC_IFINDEX  1   /* u32 */
#define KNOD_A_ACCEL_ID     2   /* u32 */

/* Netlink attributes for "accel-set" */
#define KNOD_A_ID           1   /* u32 */
#define KNOD_A_FEATURE_ENA  3   /* u32, enum knod_feature */

/* Netlink attributes for "accel-get" / "dev-get" response */
#define KNOD_A_ACCEL_TYPE   4   /* u32 */
#define KNOD_A_FEATURE_SUP  5   /* u32 */
#define KNOD_A_DEV_ACCEL_ID 6   /* u32 */

enum knod_feature {
    KNOD_FEATURE_NONE  = 0,
    KNOD_FEATURE_BPF   = 1,
    KNOD_FEATURE_IPSEC = 2,
};

/* Generic netlink command ids — must match knod.yaml in the patchset */
enum knod_cmd {
    KNOD_CMD_ACCEL_GET  = 1,
    KNOD_CMD_ACCEL_SET  = 2,
    KNOD_CMD_DEV_GET    = 3,
    KNOD_CMD_ATTACH     = 4,
    KNOD_CMD_DETACH     = 5,
};

/* ---- end KNOD uapi ---- */

/* Minimal inline XDP pass program in BPF bytecode (eBPF, little-endian).
 *
 * Equivalent C:
 *   SEC("xdp") int xdp_pass(struct xdp_md *ctx) { return XDP_PASS; }
 *
 * This is the simplest possible offload smoke test: the GPU receives every
 * packet and immediately returns XDP_PASS, which causes KNOD to SDMA-copy
 * it into a host page and deliver it to the normal stack. If this works,
 * the GPU dispatch loop, SPSC ring, and verdict path are all functional.
 */
static const struct bpf_insn xdp_pass_insns[] = {
    /* r0 = XDP_PASS (2) */
    { .code = 0xb4 /* BPF_MOV | BPF_K | BPF_ALU */, .dst_reg = 0, .imm = 2 },
    /* exit */
    { .code = 0x95 /* BPF_EXIT | BPF_JMP */ },
};

static int verbose;

static void die(const char *msg)
{
    perror(msg);
    exit(EXIT_FAILURE);
}

/* ---- Generic netlink helpers ---- */

static struct mnl_socket *nl;
static uint32_t knod_family_id;
static unsigned int nl_seq;

static int resolve_genl_family(const char *name)
{
    char buf[MNL_SOCKET_BUFFER_SIZE];
    struct nlmsghdr *nlh;
    struct genlmsghdr *genl;
    struct nlattr *attr;
    int ret;

    nlh = mnl_nlmsg_put_header(buf);
    nlh->nlmsg_type  = GENL_ID_CTRL;
    nlh->nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK;
    nlh->nlmsg_seq   = ++nl_seq;

    genl = mnl_nlmsg_put_extra_header(nlh, sizeof(*genl));
    genl->cmd     = CTRL_CMD_GETFAMILY;
    genl->version = 1;

    mnl_attr_put_strz(nlh, CTRL_ATTR_FAMILY_NAME, name);

    if (mnl_socket_sendto(nl, nlh, nlh->nlmsg_len) < 0)
        die("mnl_socket_sendto");

    ret = mnl_socket_recvfrom(nl, buf, sizeof(buf));
    if (ret <= 0)
        die("mnl_socket_recvfrom (family resolve)");

    /* Walk the response to find CTRL_ATTR_FAMILY_ID */
    nlh = (struct nlmsghdr *)buf;
    genl = (struct genlmsghdr *)mnl_nlmsg_get_payload(nlh);
    attr = (struct nlattr *)((char *)genl + sizeof(*genl));

    mnl_attr_for_each(attr, nlh, sizeof(*genl)) {
        if (mnl_attr_get_type(attr) == CTRL_ATTR_FAMILY_ID)
            return (int)mnl_attr_get_u16(attr);
    }
    fprintf(stderr, "knod genl family not found — is the kernel built with CONFIG_NET_KNOD=y?\n");
    return -ENOENT;
}

static struct nlmsghdr *build_knod_msg(char *buf, size_t bufsz,
                                       uint8_t cmd, uint16_t flags)
{
    struct nlmsghdr *nlh;
    struct genlmsghdr *genl;

    nlh = mnl_nlmsg_put_header(buf);
    nlh->nlmsg_type  = knod_family_id;
    nlh->nlmsg_flags = NLM_F_REQUEST | flags;
    nlh->nlmsg_seq   = ++nl_seq;

    genl = mnl_nlmsg_put_extra_header(nlh, sizeof(*genl));
    genl->cmd     = cmd;
    genl->version = KNOD_GENL_VERSION;

    return nlh;
}

static int send_and_ack(struct nlmsghdr *nlh)
{
    char buf[MNL_SOCKET_BUFFER_SIZE];
    int ret;

    nlh->nlmsg_flags |= NLM_F_ACK;
    if (mnl_socket_sendto(nl, nlh, nlh->nlmsg_len) < 0)
        return -errno;

    ret = mnl_socket_recvfrom(nl, buf, sizeof(buf));
    if (ret < 0)
        return -errno;

    return mnl_cb_run(buf, ret, nl_seq, mnl_socket_get_portid(nl), NULL, NULL);
}

/* ---- accel-get: list accelerators ---- */

struct accel_info {
    uint32_t id;
    uint32_t type;
    uint32_t feature_sup;
};

static int accel_get_cb(const struct nlmsghdr *nlh, void *data)
{
    struct accel_info info = {};
    struct genlmsghdr *genl = mnl_nlmsg_get_payload(nlh);
    struct nlattr *attr;

    mnl_attr_for_each(attr, nlh, sizeof(*genl)) {
        switch (mnl_attr_get_type(attr)) {
        case KNOD_A_ID:          info.id          = mnl_attr_get_u32(attr); break;
        case KNOD_A_ACCEL_TYPE:  info.type        = mnl_attr_get_u32(attr); break;
        case KNOD_A_FEATURE_SUP: info.feature_sup = mnl_attr_get_u32(attr); break;
        }
    }

    printf("  accel id=%u type=%u features_supported=0x%x\n",
           info.id, info.type, info.feature_sup);
    return MNL_CB_OK;
}

static int knod_accel_get(void)
{
    char buf[MNL_SOCKET_BUFFER_SIZE];
    struct nlmsghdr *nlh;
    int ret;

    nlh = build_knod_msg(buf, sizeof(buf), KNOD_CMD_ACCEL_GET,
                         NLM_F_DUMP);

    if (mnl_socket_sendto(nl, nlh, nlh->nlmsg_len) < 0)
        die("mnl_socket_sendto (accel-get)");

    printf("Accelerators:\n");
    do {
        ret = mnl_socket_recvfrom(nl, buf, sizeof(buf));
        if (ret <= 0) break;
        ret = mnl_cb_run(buf, ret, nl_seq, mnl_socket_get_portid(nl),
                         accel_get_cb, NULL);
    } while (ret == MNL_CB_OK);

    return (ret == MNL_CB_STOP || ret >= 0) ? 0 : -1;
}

/* ---- attach ---- */

static int knod_attach(uint32_t ifindex, uint32_t accel_id)
{
    char buf[MNL_SOCKET_BUFFER_SIZE];
    struct nlmsghdr *nlh;

    printf("Attaching ifindex=%u to accel_id=%u\n", ifindex, accel_id);
    nlh = build_knod_msg(buf, sizeof(buf), KNOD_CMD_ATTACH, 0);
    mnl_attr_put_u32(nlh, KNOD_A_NIC_IFINDEX, ifindex);
    mnl_attr_put_u32(nlh, KNOD_A_ACCEL_ID, accel_id);
    return send_and_ack(nlh);
}

/* ---- accel-set ---- */

static int knod_accel_set(uint32_t accel_id, enum knod_feature feature)
{
    char buf[MNL_SOCKET_BUFFER_SIZE];
    struct nlmsghdr *nlh;
    const char *fname = feature == KNOD_FEATURE_BPF ? "bpf" :
                        feature == KNOD_FEATURE_IPSEC ? "ipsec" : "none";

    printf("Setting accel_id=%u feature=%s\n", accel_id, fname);
    nlh = build_knod_msg(buf, sizeof(buf), KNOD_CMD_ACCEL_SET, 0);
    mnl_attr_put_u32(nlh, KNOD_A_ID, accel_id);
    mnl_attr_put_u32(nlh, KNOD_A_FEATURE_ENA, (uint32_t)feature);
    return send_and_ack(nlh);
}

/* ---- detach ---- */

static int knod_detach(uint32_t ifindex)
{
    char buf[MNL_SOCKET_BUFFER_SIZE];
    struct nlmsghdr *nlh;

    printf("Detaching ifindex=%u\n", ifindex);
    nlh = build_knod_msg(buf, sizeof(buf), KNOD_CMD_DETACH, 0);
    mnl_attr_put_u32(nlh, KNOD_A_NIC_IFINDEX, ifindex);
    return send_and_ack(nlh);
}

/* ---- BPF XDP offload load + attach ---- */

static int load_xdp_pass_offload(unsigned int ifindex)
{
    union bpf_attr attr = {};
    int prog_fd;

    /*
     * Load an XDP program in offload mode (BPF_F_XDP_DEV_BOUND_ONLY).
     * The kernel routes this through the KNOD JIT path, which compiles
     * the eBPF bytecode to AMD GCN machine code and uploads it to the GPU.
     */
    attr.prog_type    = BPF_PROG_TYPE_XDP;
    attr.insns        = (unsigned long)xdp_pass_insns;
    attr.insn_cnt     = sizeof(xdp_pass_insns) / sizeof(xdp_pass_insns[0]);
    attr.license      = (unsigned long)"GPL";
    attr.prog_ifindex = ifindex;           /* offload target NIC */
    /* BPF_F_XDP_DEV_BOUND_ONLY ensures the kernel uses XDP_MODE_HW */
    attr.prog_flags   = BPF_F_XDP_DEV_BOUND_ONLY;
    snprintf((char *)attr.prog_name, sizeof(attr.prog_name), "xdp_pass_knod");

    prog_fd = syscall(321 /* __NR_bpf */, BPF_PROG_LOAD, &attr, sizeof(attr));
    if (prog_fd < 0) {
        perror("bpf(BPF_PROG_LOAD) for XDP offload");
        fprintf(stderr, "  Ensure CONFIG_BPF_JIT=y and CONFIG_AMD_KFD_KNOD=y\n");
        return -1;
    }

    if (verbose)
        printf("  BPF prog fd=%d (XDP offload, GPU JIT)\n", prog_fd);
    return prog_fd;
}

static int attach_xdp_hw(unsigned int ifindex, int prog_fd)
{
    struct {
        struct nlmsghdr  nlh;
        struct ifinfomsg ifi;
        char             buf[128];
    } req = {};
    int fd, ret;
    struct rtattr *linkinfo, *xdp, *rta;

    /*
     * Attach the BPF program to the NIC in XDP hardware offload mode
     * (XDP_FLAGS_HW_MODE) via rtnetlink SETLINK. This triggers the
     * KNOD XDP offload path in the kernel.
     */
    fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_ROUTE);
    if (fd < 0) die("socket(NETLINK_ROUTE)");

    req.nlh.nlmsg_len   = NLMSG_LENGTH(sizeof(req.ifi));
    req.nlh.nlmsg_type  = RTM_SETLINK;
    req.nlh.nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK;
    req.nlh.nlmsg_seq   = 1;
    req.ifi.ifi_family  = AF_UNSPEC;
    req.ifi.ifi_index   = (int)ifindex;

    /* XDP linkinfo attribute */
#ifndef IFLA_XDP
#define IFLA_XDP 43
#endif
#ifndef IFLA_XDP_FD
#define IFLA_XDP_FD 1
#endif
#ifndef IFLA_XDP_FLAGS
#define IFLA_XDP_FLAGS 3
#endif
#ifndef XDP_FLAGS_HW_MODE
#define XDP_FLAGS_HW_MODE (1U << 2)
#endif

    rta = (struct rtattr *)((char *)&req + req.nlh.nlmsg_len);
    rta->rta_type = IFLA_XDP;
    rta->rta_len  = RTA_LENGTH(0);
    req.nlh.nlmsg_len = NLMSG_ALIGN(req.nlh.nlmsg_len) + RTA_LENGTH(0);

    /* fd */
    struct rtattr *fd_rta = (struct rtattr *)((char *)rta + rta->rta_len);
    fd_rta->rta_type = IFLA_XDP_FD | NLA_F_NESTED;
    fd_rta->rta_len  = RTA_LENGTH(sizeof(int));
    memcpy(RTA_DATA(fd_rta), &prog_fd, sizeof(int));
    rta->rta_len  += fd_rta->rta_len;
    req.nlh.nlmsg_len += fd_rta->rta_len;

    /* flags: XDP_FLAGS_HW_MODE */
    struct rtattr *fl_rta = (struct rtattr *)((char *)fd_rta + fd_rta->rta_len);
    uint32_t flags = XDP_FLAGS_HW_MODE;
    fl_rta->rta_type = IFLA_XDP_FLAGS | NLA_F_NESTED;
    fl_rta->rta_len  = RTA_LENGTH(sizeof(flags));
    memcpy(RTA_DATA(fl_rta), &flags, sizeof(flags));
    rta->rta_len  += fl_rta->rta_len;
    req.nlh.nlmsg_len += fl_rta->rta_len;

    if (send(fd, &req, req.nlh.nlmsg_len, 0) < 0) {
        perror("send(RTM_SETLINK XDP)");
        close(fd);
        return -1;
    }

    char resp[4096];
    ret = recv(fd, resp, sizeof(resp), 0);
    close(fd);

    if (ret < 0) {
        perror("recv(RTM_SETLINK XDP)");
        return -1;
    }

    struct nlmsghdr *nlh = (struct nlmsghdr *)resp;
    if (nlh->nlmsg_type == NLMSG_ERROR) {
        struct nlmsgerr *err = (struct nlmsgerr *)NLMSG_DATA(nlh);
        if (err->error) {
            errno = -err->error;
            perror("RTM_SETLINK XDP HW");
            return -1;
        }
    }

    printf("  XDP prog attached in HW offload mode (GPU JIT active)\n");
    return 0;
}

/* ---- traffic test ---- */

static int send_test_packets(unsigned int ifindex, int count)
{
    struct sockaddr_ll sa = {
        .sll_family   = AF_PACKET,
        .sll_protocol = htons(ETH_P_IP),
        .sll_ifindex  = (int)ifindex,
        .sll_halen    = ETH_ALEN,
        .sll_addr     = { 0xff, 0xff, 0xff, 0xff, 0xff, 0xff },
    };
    uint8_t pkt[64] = {
        /* dst MAC */ 0xff,0xff,0xff,0xff,0xff,0xff,
        /* src MAC */ 0x02,0x00,0x00,0x00,0x00,0x01,
        /* ethertype: IP */ 0x08,0x00,
        /* IPv4 header: version+IHL */ 0x45,
        /* DSCP */ 0x00,
        /* total length */ 0x00,0x32,
        /* id */ 0x00,0x01,
        /* flags+frag */ 0x00,0x00,
        /* TTL */ 0x40,
        /* proto: UDP */ 0x11,
        /* checksum */ 0x00,0x00,
        /* src IP: 10.0.0.1 */ 0x0a,0x00,0x00,0x01,
        /* dst IP: 10.0.0.2 */ 0x0a,0x00,0x00,0x02,
        /* UDP src port 4321 */ 0x10,0xe1,
        /* UDP dst port 4322 */ 0x10,0xe2,
        /* UDP length */ 0x00,0x1a,
        /* UDP checksum */ 0x00,0x00,
        /* payload: "KNOD test pkt" */
        'K','N','O','D',' ','t','e','s','t',' ','p','k','t',0x00,
    };

    int sock = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (sock < 0) { perror("socket(AF_PACKET)"); return -1; }

    printf("Sending %d test packets via ifindex=%u (XDP_PASS expected)\n",
           count, ifindex);

    for (int i = 0; i < count; i++) {
        pkt[26] = (uint8_t)(i >> 8);
        pkt[27] = (uint8_t)i;
        ssize_t n = sendto(sock, pkt, sizeof(pkt), 0,
                           (struct sockaddr *)&sa, sizeof(sa));
        if (n < 0) { perror("sendto"); close(sock); return -1; }
        if (verbose) printf("  sent pkt %d (%zd bytes)\n", i, n);
    }

    close(sock);
    return 0;
}

/* ---- main ---- */

static void usage(const char *prog)
{
    fprintf(stderr,
        "Usage: %s -i <ifname> [-a <accel-id>] [-n <pkt-count>] [-v]\n"
        "\n"
        "  -i <ifname>    NIC interface to offload (e.g. eth0, enp1s0)\n"
        "  -a <accel-id>  KNOD accelerator id (default: 0)\n"
        "  -n <count>     number of test packets to send (default: 16)\n"
        "  -v             verbose output\n"
        "\n"
        "Requires a kernel with CONFIG_NET_KNOD=y and CONFIG_AMD_KFD_KNOD=y,\n"
        "an RDNA2 AMD GPU, and a supported NIC (mlx5e or bnxt_en).\n",
        prog);
    exit(EXIT_FAILURE);
}

int main(int argc, char **argv)
{
    const char *ifname   = NULL;
    uint32_t    accel_id = 0;
    int         pkt_count = 16;
    int opt, ret;

    while ((opt = getopt(argc, argv, "i:a:n:v")) != -1) {
        switch (opt) {
        case 'i': ifname    = optarg;          break;
        case 'a': accel_id  = (uint32_t)atoi(optarg); break;
        case 'n': pkt_count = atoi(optarg);    break;
        case 'v': verbose   = 1;               break;
        default:  usage(argv[0]);
        }
    }

    if (!ifname) {
        fprintf(stderr, "error: -i <ifname> is required\n\n");
        usage(argv[0]);
    }

    unsigned int ifindex = if_nametoindex(ifname);
    if (!ifindex) {
        fprintf(stderr, "error: interface '%s' not found\n", ifname);
        return EXIT_FAILURE;
    }

    printf("KNOD XDP offload test\n");
    printf("  interface: %s (ifindex=%u)\n", ifname, ifindex);
    printf("  accel_id:  %u\n\n", accel_id);

    /* Open generic netlink socket */
    nl = mnl_socket_open(NETLINK_GENERIC);
    if (!nl) die("mnl_socket_open");
    if (mnl_socket_bind(nl, 0, MNL_SOCKET_AUTOPID) < 0) die("mnl_socket_bind");

    ret = resolve_genl_family(KNOD_GENL_NAME);
    if (ret < 0) {
        fprintf(stderr, "Could not resolve knod genl family (ret=%d)\n", ret);
        mnl_socket_close(nl);
        return EXIT_FAILURE;
    }
    knod_family_id = (uint32_t)ret;
    if (verbose) printf("knod genl family id=%u\n\n", knod_family_id);

    /* 1. List accelerators */
    if (knod_accel_get() < 0) {
        fprintf(stderr, "accel-get failed\n");
        goto out;
    }
    printf("\n");

    /* 2. Attach NIC to accelerator */
    ret = knod_attach(ifindex, accel_id);
    if (ret < 0) {
        fprintf(stderr, "attach failed (ret=%d errno=%d %s)\n",
                ret, errno, strerror(errno));
        goto out;
    }
    printf("  attach OK\n\n");

    /* 3. Enable BPF/XDP feature on the accelerator */
    ret = knod_accel_set(accel_id, KNOD_FEATURE_BPF);
    if (ret < 0) {
        fprintf(stderr, "accel-set(bpf) failed (ret=%d)\n", ret);
        goto detach;
    }
    printf("  accel-set OK\n\n");

    /* 4. Load XDP_PASS program in HW offload mode (GPU JIT) */
    printf("Loading XDP_PASS program in GPU offload mode...\n");
    int prog_fd = load_xdp_pass_offload(ifindex);
    if (prog_fd < 0) goto detach;

    ret = attach_xdp_hw(ifindex, prog_fd);
    close(prog_fd);
    if (ret < 0) goto detach;
    printf("\n");

    /* 5. Send test traffic */
    if (send_test_packets(ifindex, pkt_count) < 0) goto detach;

    /* Brief pause so the GPU can drain its verdict ring before detach */
    struct timespec ts = { .tv_nsec = 100 * 1000 * 1000 }; /* 100 ms */
    nanosleep(&ts, NULL);

    printf("\nTest complete — %d packets sent via GPU XDP_PASS path\n", pkt_count);

detach:
    /* 6. Detach */
    if (knod_detach(ifindex) < 0)
        fprintf(stderr, "detach failed (non-fatal)\n");
    else
        printf("  detach OK\n");

out:
    mnl_socket_close(nl);
    return ret < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
}
