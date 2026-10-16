#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
lb config --distribution trixie --architectures amd64 --binary-images iso-hybrid \
 --debian-installer false --archive-areas main --apt-recommends false --apt-indices false \
 --memtest none --bootloaders 'syslinux,grub-efi' --checksums sha256 \
 --iso-volume VIRTUALWAN_RESCUE --image-name virtualwan-rescue \
 --bootappend-live 'boot=live components hostname=virtualwan-rescue username=rescue locales=C.UTF-8 keyboard-layouts=fr noeject nopersistence ip=none noautologin console=tty0 console=ttyS0,115200n8' \
 --mirror-bootstrap https://deb.debian.org/debian/ --mirror-chroot https://deb.debian.org/debian/ \
 --mirror-binary https://deb.debian.org/debian/ --mirror-chroot-security https://security.debian.org/debian-security/ \
 --mirror-binary-security https://security.debian.org/debian-security/
mkdir -p config/package-lists config/includes.chroot/usr/local/sbin config/includes.chroot/etc/systemd/system/multi-user.target.wants config/includes.chroot/etc/ssh/sshd_config.d config/hooks/live
cp /source/packages.list config/package-lists/rescue.list.chroot
cp /source/network.py config/includes.chroot/usr/local/sbin/virtualwan-rescue-network
cp /source/auth.py config/includes.chroot/usr/local/sbin/virtualwan-rescue-auth
chmod 755 config/includes.chroot/usr/local/sbin/virtualwan-rescue-*
cp /source/virtualwan-rescue.service /source/virtualwan-rescue-auth.service config/includes.chroot/etc/systemd/system/
ln -s ../virtualwan-rescue-auth.service config/includes.chroot/etc/systemd/system/multi-user.target.wants/virtualwan-rescue-auth.service
mkdir -p config/includes.binary
printf '%s\n' '{"credential_protocol":1,"name":"Virtualwan Rescue"}' > config/includes.binary/virtualwan-rescue.json
ln -s ../virtualwan-rescue.service config/includes.chroot/etc/systemd/system/multi-user.target.wants/virtualwan-rescue.service
printf '%s\n' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' 'PermitRootLogin no' 'AllowUsers rescue' > config/includes.chroot/etc/ssh/sshd_config.d/00-virtualwan-rescue.conf
cp /source/0100-rescue.hook.chroot /source/0900-autoboot.hook.binary config/hooks/live/
chmod 755 config/hooks/live/*
lb build
mkdir -p /output
cp virtualwan-rescue-amd64.hybrid.iso /output/virtualwan-rescue-debian13-amd64-v3.iso
cp virtualwan-rescue-amd64.packages /output/packages.txt
(cd /output && sha256sum virtualwan-rescue-debian13-amd64-v3.iso > virtualwan-rescue-debian13-amd64-v3.iso.sha256)
