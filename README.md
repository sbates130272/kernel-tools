# Stephen's Kernel Tools

This repo contains a few scripts I find useful for kernel hacking.

## Installation

I normally have a top-level folder called kernel which I place this
repo into. I then have a number of sub-folders:

+ **```src```**: The [Linux Kernel][1] tree.

+ **```useful-configs```**: A folder for some good kernel configs. For
example this includes a really small (but valid) config, a QEMU
specific config and an Ubuntu 18.04 like config.

+ **```configs```**: A folder for all the other configs I use, I usually
don't repo this or tie it to a separate private repo.

+ **```debs```**: A folder of handy output .debs (I almost always work
on Debian packages and not RPMs). Again I don't repo this.

## build-kernel-debrpm

```build-kernel-debrpm``` is a shell script that builds a kernel
debian package or RHEL/CentOS RPM files for installation of a linux
kernel. See the notes in the file for more information. Note this
script lives in the scripts folder.

An example run of this script that would build a p2pdma enabled kernel
based on version 4 of the patchsets would be:
```
REMOTE=https://github.com/sbates130272/linux-p2pmem \
  REMOTE_BRANCH=pci-p2p-v4 \
  CONFIG=<config-file> \
  ./build-kernel-debrpm
```
And an example that builds a monolithic kernel based on the default
config (with all modules converted to "yes") for v4.14.30 would be:
```
REMOTE= git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable.git \
  REMOTE_BRANCH=v4.14.30 \
  MONO=yes \
  ./build-kernel-debrpm
```
And an example that builds a non-monolithic kernel based on the default
config (with all modules converted to "yes") for v4.14.30 for arm64 on
a non-arm64 host system would be:
```
REMOTE= git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable.git \
  REMOTE_BRANCH=v4.14.30 \
  ARCH=arm64 \
  CROSS_COMPILE=aarch64-linux-gnu- \
  ./build-kernel-debrpm
```
Note all files associated with the unique generation of this kernel
(the .config and the <path-file> (if present) are included in the
output tarball along with a ```.build-info file``` that contains the
information needed to reproduce the .debs or .rpms.

## build-latest-p2pdma-kernel

A helper script around ```build-kernel-debrpm``` that should always
point to the latest and greatest p2pdma kernel. Run this to generate a
p2pdma kernel unless you know what you are doing and want to use
build-kernel-debrpm. Only supports x86_64 right now.

### Patches

```build-latest-p2pdma-kernel``` uses three patches (located in the
patches folder) to address some issues with p2pdma kernels. Namely:

+ **acs_disable**: Add a kernel configuration parameter
(```acs_disable```) that turns off [PCIe ACS][2] everywhere. This can
be useful for testing p2pdma. To enable this mode add
```pci=acs_disable``` to your kernel command line parameters.

+ **p2pmem-pci**: Adds a device driver for p2pmem exposed by a device
driver. Based on [this repository][3]. We use this to expose p2pmem to
userspace applications.

+ **p2pdma patches**: A couple of changes were made in ```p2pdma.c```
in the v5.4 kernel that broke out of tree drivers for p2pmem. These
patches fix this. They are not needed for pre-v5.4 kernels.

**Note that the current version of the ```patches``` sub-folder this
repository only supports v5.6.x series kernel source due to changes in
```drivers/pci/probe.c```. You will need to generate your own patches
to address other kernels. We plan to address this issue soon!**

## docker

A Dockerfile exists that can generate the enviromnent needed to run
```build-latest-p2pdma-kernel```. This can make the generation of the
.deb for this kernel simpler for some users. To generate the kernel
Debian packages this way proceed as follows from the top-level folder
of this repo. Note that since we always pull from the HEAD of the
repository we need to run docker with ```-no-cache```. This slows down
the build time but avoids incorrect builds.
```
cd docker
docker build --no-cache -t kernel-tools:latest .
```
once the image has run to completion you can obtain the tarball via
the following (from either the docker folder or the top-level folder
for this repo).
```
docker create --name kernel-tools kernel-tools:latest
docker cp kernel-tools:/kernel/build-kernel-deb.docker.tar.gz .
docker rm kernel-tools
```
You can then install the .deb using the same approach as a native
build using build-latest-p2pdma-kernel.

## arch-tools

The ```arch-tools``` sub-folder contains a few useful tools for
building Linux kernels and kernel modules for non-amd64 based
systems. This includes some platform-specific tools and some generic
ISA tools.

One of the most useful of these tools is the ```build-arm64``` script
which can build a ARM64 kernel and modules using a cross-compiler
approach. You should call this script from the top-level of a kernel
source tree. See the header of the script for more information. There
is also a ```build-riscv``` script which can do something similar for
riscv32 and riscv64 ISA.

[1]: https://www.kernel.org/
[2]: http://www.intel.com/content/www/us/en/pci-express/pci-sig-sr-iov-primer-sr-iov-technology-paper.html
[3]: https://github.com/Eideticom/p2pmem-pci
