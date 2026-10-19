#!/bin/bash
#
# (c) Stephen Bates, 2026
#
# Spin up a transient QEMU VM with one emulated NVMe device using
# qemu-tool, then install a DMA-BUF kernel (Anuj Gupta's
# fs-dmabuf-rfc-v11-desc-v2 branch: io_uring v11 + iomap/XFS filesystem
# DMA-BUF support) via scripts/build-remote, and finally verify the
# feature is present.
#
# Prerequisites:
#   pip install qemu-tool    (or install from the qemu-minimal repo)
#   # qemu-tool pulls the Ubuntu cloud image on first gen-vm run
#
# The script performs these steps:
#   1. Create a VM image with qemu-tool gen-vm (downloads Ubuntu cloud
#      image if not already cached in VM_IMAGES_DIR).
#   2. Start the VM with qemu-tool run-vm --nvme 1 (one emulated NVMe).
#   3. Wait for SSH to become available.
#   4. Clone the kernel source tree from the anuj fs-dmabuf branch.
#   5. Run scripts/build-remote from inside that tree, targeting the VM.
#   6. Wait for the VM to come back after the reboot build-remote triggers.
#   7. Verify the new kernel is running and print the NVMe device.
#   8. On exit stop the VM and remove the overlay image.
#
# Override any variable on the command line, e.g.:
#
#   VM_NAME=dmabuf-test \
#   VM_SSH_PORT=2223 \
#   VM_IMAGES_DIR=/var/lib/qemu-tool/images \
#   KERNEL_BRANCH=anuj/fs-dmabuf-rfc-v11-desc-v3 \
#   ./scripts/run-dmabuf-vm-test.sh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VM_NAME=${VM_NAME:-dmabuf-test}
VM_IMAGES_DIR=${VM_IMAGES_DIR:-/var/lib/qemu-tool/images}
VM_VCPUS=${VM_VCPUS:-4}
VM_VMEM=${VM_VMEM:-4096}
VM_SSH_PORT=${VM_SSH_PORT:-2222}
VM_RELEASE=${VM_RELEASE:-resolute}
VM_SSH_KEY_FILE=${VM_SSH_KEY_FILE:-${HOME}/.ssh/id_ed25519.pub}

KERNEL_REMOTE=${KERNEL_REMOTE:-https://github.com/anuj7781/linux-kernel}
KERNEL_BRANCH=${KERNEL_BRANCH:-anuj/fs-dmabuf-rfc-v11-desc-v2}
KERNEL_SRC_DIR=${KERNEL_SRC_DIR:-/tmp/linux-dmabuf-$$}

CONFIG_SCRIPT=${CONFIG_SCRIPT:-default}
LABEL=${LABEL:--dmabuf-v11-fs}

SSH_ALIAS=${VM_NAME}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() { echo "[$(date +%H:%M:%S)] $*"; }

VM_PID=""

cleanup() {
    log "Cleaning up..."
    if [ -n "${VM_PID}" ] && kill -0 "${VM_PID}" 2>/dev/null; then
        log "Stopping VM (pid ${VM_PID})..."
        kill "${VM_PID}" || true
        wait "${VM_PID}" 2>/dev/null || true
    fi
    # Remove the transient SSH config block
    if [ -f "${HOME}/.ssh/config" ]; then
        sed -i "/^# BEGIN ${SSH_ALIAS}-$$/,/^# END ${SSH_ALIAS}-$$/d" \
            "${HOME}/.ssh/config" 2>/dev/null || true
    fi
    rm -rf "${KERNEL_SRC_DIR}"
    log "Done."
}
trap cleanup EXIT

ssh_vm() {
    ssh -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=3 \
        -o BatchMode=yes \
        -p "${VM_SSH_PORT}" \
        -i "${HOME}/.ssh/id_ed25519" \
        "ubuntu@localhost" "$@"
}

wait_for_ssh() {
    local timeout=${1:-180}
    local elapsed=0
    log "Waiting for SSH on port ${VM_SSH_PORT} (up to ${timeout}s)..."
    until ssh_vm true 2>/dev/null; do
        sleep 3
        elapsed=$((elapsed + 3))
        [ "${elapsed}" -lt "${timeout}" ] || {
            echo "ERROR: VM did not respond on SSH within ${timeout}s." >&2
            exit 1
        }
    done
    log "SSH is up."
}

# ---------------------------------------------------------------------------
# Step 1: Sanity checks
# ---------------------------------------------------------------------------

log "=== Step 1: Sanity checks ==="

for cmd in qemu-tool git ssh; do
    command -v "${cmd}" &>/dev/null || {
        echo "ERROR: Required command not found: ${cmd}" >&2
        exit 1
    }
done

mkdir -p "${VM_IMAGES_DIR}"

# ---------------------------------------------------------------------------
# Step 2: Generate VM image (downloads cloud image on first run)
# ---------------------------------------------------------------------------

log "=== Step 2: Generating VM image '${VM_NAME}' (release: ${VM_RELEASE}) ==="

qemu-tool gen-vm \
    --vm-name    "${VM_NAME}" \
    --images     "${VM_IMAGES_DIR}" \
    --vcpus      "${VM_VCPUS}" \
    --vmem       "${VM_VMEM}" \
    --ssh-port   "${VM_SSH_PORT}" \
    --release    "${VM_RELEASE}" \
    --ssh-key-file "${VM_SSH_KEY_FILE}" \
    --force

# ---------------------------------------------------------------------------
# Step 3: Start the VM with one emulated NVMe device
# ---------------------------------------------------------------------------

log "=== Step 3: Starting VM with 1 emulated NVMe device ==="

qemu-tool run-vm \
    --vm-name  "${VM_NAME}" \
    --images   "${VM_IMAGES_DIR}" \
    --vcpus    "${VM_VCPUS}" \
    --vmem     "${VM_VMEM}" \
    --ssh-port "${VM_SSH_PORT}" \
    --nvme     1 \
    &
VM_PID=$!
log "QEMU running in background (pid ${VM_PID})."

# ---------------------------------------------------------------------------
# Step 4: Wait for SSH and add a config alias
# ---------------------------------------------------------------------------

log "=== Step 4: Waiting for SSH ==="
wait_for_ssh 240

# Add a transient Host block so build-remote can reach the VM by name.
cat >> "${HOME}/.ssh/config" << SSHCONF

# BEGIN ${SSH_ALIAS}-$$
Host ${SSH_ALIAS}
    HostName localhost
    Port ${VM_SSH_PORT}
    User ubuntu
    IdentityFile ${HOME}/.ssh/id_ed25519
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
# END ${SSH_ALIAS}-$$
SSHCONF

log "SSH alias '${SSH_ALIAS}' added."

# ---------------------------------------------------------------------------
# Step 5: Discover the boot config name that build-remote needs
# ---------------------------------------------------------------------------

log "=== Step 5: Discovering VM boot config ==="

REMOTE_UNAME=$(ssh_vm uname -r)
log "VM uname: ${REMOTE_UNAME}"

# Wait for cloud-init to finish so packages (kernel headers etc.) are installed
ssh_vm "cloud-init status --wait 2>/dev/null || true"

# Find the config file -- resolute/noble cloud images ship it at /boot/config-<uname>
VM_CONFIG="config-${REMOTE_UNAME}"
ssh_vm "test -f /boot/${VM_CONFIG}" || {
    # Fallback: pick whatever config is present
    VM_CONFIG=$(ssh_vm "ls /boot/config-* 2>/dev/null | head -1 | sed 's|/boot/||'")
    log "Resolved config file: ${VM_CONFIG}"
}
log "Using CONFIG=${VM_CONFIG}"

# ---------------------------------------------------------------------------
# Step 6: Clone kernel source
# ---------------------------------------------------------------------------

log "=== Step 6: Cloning kernel (${KERNEL_BRANCH}) ==="

git clone --depth 1 "${KERNEL_REMOTE}" -b "${KERNEL_BRANCH}" "${KERNEL_SRC_DIR}"

# ---------------------------------------------------------------------------
# Step 7: Run build-remote
# ---------------------------------------------------------------------------

log "=== Step 7: Running build-remote ==="

(
    cd "${KERNEL_SRC_DIR}"
    REMOTE_NAME="${SSH_ALIAS}" \
    CONFIG="${VM_CONFIG}" \
    CONFIG_SCRIPT="${CONFIG_SCRIPT}" \
    LABEL="${LABEL}" \
        "${REPO_DIR}/scripts/build-remote"
)

# build-remote issues 'sudo reboot now' as its last step.

# ---------------------------------------------------------------------------
# Step 8: Wait for VM to come back and verify
# ---------------------------------------------------------------------------

log "=== Step 8: Verifying new kernel ==="

sleep 10   # give the reboot a moment to begin
wait_for_ssh 240

NEW_UNAME=$(ssh_vm uname -r)
log "Kernel after reboot: ${NEW_UNAME}"

if [[ "${NEW_UNAME}" != *"${LABEL#-}"* ]]; then
    echo "WARNING: label '${LABEL}' not found in uname '${NEW_UNAME}'." >&2
    echo "  Check /boot/grub/grub.cfg default on the VM." >&2
fi

log "DMA-BUF / io_uring config knobs:"
ssh_vm "zcat /proc/config.gz 2>/dev/null | grep -E 'IO_URING|DMA_BUF|XFS' \
    || zgrep -E 'IO_URING|DMA_BUF|XFS' /boot/config-\$(uname -r) 2>/dev/null \
    | head -20"

log "NVMe devices:"
ssh_vm "lsblk -d -o NAME,MODEL,SIZE | grep -i nvme || echo '  (none visible yet)'"

log ""
log "=== All steps complete ==="
log "VM is running on localhost:${VM_SSH_PORT} (SSH alias: ${SSH_ALIAS})."
log "Connect with:  ssh ${SSH_ALIAS}"
log ""
log "When you are done, press Ctrl-C to stop the VM and clean up."

wait "${VM_PID}" || true
