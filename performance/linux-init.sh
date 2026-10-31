#!/bin/sh
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
set -eu
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
for module in virtio_mmio virtio_scsi sd_mod vfat; do
    modprobe "$module"
done
attempt=0
while [ ! -b /dev/sda1 ]; do
    attempt=$((attempt + 1))
    [ "$attempt" -le 30 ] || exit 1
    sleep 1
done
mkdir -p /data
mount -t vfat -o rw,noatime /dev/sda1 /data
uname -a
cat /proc/mounts
echo BENCH-READY
exec /bin/sh -i
