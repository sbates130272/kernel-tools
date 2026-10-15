# docker/amdgpu-dkms.hcl
#
# docker buildx bake file for the amdgpu-dkms build image.
#
# Usage (build image for a specific kernel):
#   docker buildx bake --builder default \
#     --set amdgpu-dkms-build.args.KERNEL_VER=6.8.0-142-generic \
#     -f docker/amdgpu-dkms.hcl
#
# Usage (build all kernel variants defined below):
#   docker buildx bake --builder default -f docker/amdgpu-dkms.hcl
#
# The --builder default flag is required on AMD corporate networks because
# the ZScaler TLS proxy makes the remote docker-container builder fail to
# resolve image metadata (x509 certificate signed by unknown authority).
# The default builder uses the local Docker daemon's BuildKit, which works
# correctly behind ZScaler.
#
# BuildKit cache mounts in the Dockerfile keep the apt lists and .deb files
# between builds so rebuilding after a Dockerfile change is fast (~3s vs 2m).

variable "KERNEL_VER" {
  default = "6.8.0-142-generic"
}

# Multiple kernel variants can be baked in parallel by listing them here.
# Run `docker buildx bake --builder default -f docker/amdgpu-dkms.hcl` to
# build all of them in parallel.
group "default" {
  targets = ["amdgpu-dkms-build"]
}

group "all-kernels" {
  targets = [
    "amdgpu-dkms-build-6-8-0-142-generic",
    "amdgpu-dkms-build-6-8-0-146-generic",
    # Latest Ubuntu packaging of the upstream 7.x stable series.
    # CI (docker-fast-build-amdgpu-test.yml) auto-detects the current version
    # at run time; this target pins a known-good version for local bake use.
    # Update when Ubuntu ships a newer header package:
    #   apt-cache search 'linux-headers-7\.' | grep generic | sort -V | tail -1
    "amdgpu-dkms-build-7-0-0-38-generic",
  ]
}

# Base target — used as a template by the per-kernel targets below.
target "amdgpu-dkms-base" {
  context    = "."
  dockerfile = "docker/Dockerfile.amdgpu-dkms"
  # BuildKit apt cache mounts persist between builds; the --builder default
  # daemon keeps them in its own cache storage.
}

# Default single-kernel target controlled by the KERNEL_VER variable.
target "amdgpu-dkms-build" {
  inherits = ["amdgpu-dkms-base"]
  args = {
    KERNEL_VER = "${KERNEL_VER}"
  }
  tags = ["amdgpu-dkms-build:${KERNEL_VER}"]
}

# Per-kernel targets for parallel baking.
target "amdgpu-dkms-build-6-8-0-142-generic" {
  inherits = ["amdgpu-dkms-base"]
  args = {
    KERNEL_VER = "6.8.0-142-generic"
  }
  tags = ["amdgpu-dkms-build:6.8.0-142-generic"]
}

target "amdgpu-dkms-build-6-8-0-146-generic" {
  inherits = ["amdgpu-dkms-base"]
  args = {
    KERNEL_VER = "6.8.0-146-generic"
  }
  tags = ["amdgpu-dkms-build:6.8.0-146-generic"]
}

target "amdgpu-dkms-build-7-0-0-38-generic" {
  inherits = ["amdgpu-dkms-base"]
  args = {
    KERNEL_VER = "7.0.0-38-generic"
  }
  tags = ["amdgpu-dkms-build:7.0.0-38-generic"]
}
