# Stephen's Kernel Tools

This repo contains a few scripts I find useful for kernel hacking.

## Installation

I normally have a top-level folder called kernel which I place this
repo into. I then have a number of sub-folders:

+ **```linux-<arch>```**: The [Linux Kernel][1] tree with different
directories for each architecture I am working on (e.g. arm64, riscv,
x86 etc).

+ **```configs```**: A folder for all the configs I use, I usually
don't repo this. 

+ **```debs```**: Afolder of handy output .debs (I almost always work
on Debian packages and not RPMs). Again I don't repo this.

## build-kernel-debrpm

```build-kernel-debrpm``` is a shell script that builds a kernel
debian package or RHEL/CentOS RPM files for installation of a linux
kernel. See the notes in the file for more information.

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

[1]: https://www.kernel.org/
