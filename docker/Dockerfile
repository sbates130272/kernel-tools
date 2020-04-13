# A Dockerfile that generates the latest p2pdma .deb from inside a
# docker container. Since we do this inside docker we have a lot of
# control on which distro and tooling we use. Useful for testing and
# for when the distro you use does not support the
# build-latest-p2pdma-kernel script.
#
# Once this container build runs to completion you can use the docker
# copy command (docker cp) to copy the .deb files to the host and use
# dpkg -i to do the package install on your system. See the top-level
# README.md for more information.

  # Change this to your preferred distro.
FROM ubuntu:18.04

  # Note this list may change depending on the distro chosen.
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    flex \
    bison \
    libelf-dev \
    bc \
    kmod \
    cpio \
    libssl-dev \
    rsync

  # Note you may want to edit this depending on what you want to build
  # and the capabilities of your system. 
 
RUN git clone https://github.com/sbates130272/kernel-tools.git kernel
WORKDIR kernel
RUN ./build-latest-p2pdma-kernel
RUN cp build-kernel-deb.*.tar.gz build-kernel-deb.docker.tar.gz
