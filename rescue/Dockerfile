FROM debian:13-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends live-build debootstrap xorriso isolinux syslinux-common grub-pc-bin grub-efi-amd64-bin mtools dosfstools ca-certificates qemu-system-x86 qemu-utils ovmf python3 openssh-client && rm -rf /var/lib/apt/lists/*
COPY rescue/ /source/
WORKDIR /work
CMD ["bash", "/source/build.sh"]
