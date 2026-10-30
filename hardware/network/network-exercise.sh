#!/bin/sh
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

set -eu

case "${1:-serve}" in
    serve|stats) mode=${1:-serve} ;;
    *) echo 'usage: network-exercise [serve|stats] [PORT]' >&2; exit 2 ;;
esac
port=${2:-5201}
case "$port" in
    ''|*[!0-9]*) echo 'invalid TCP port' >&2; exit 2 ;;
esac
[ "$#" -le 2 ] && [ "${#port}" -le 5 ] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || exit 2

echo 'NETWORK,METADATA,BEGIN'
uname -a
iperf3 --version
ip -4 addr show
ip route show
# BusyBox ip does not implement iproute2's -s option. The kernel counter
# interface below provides RX/TX bytes, packets, errors and drops directly.
cat /proc/net/dev
for interface in /sys/class/net/*; do
    [ -r "$interface/device/features" ] || continue
    printf 'NETWORK,VIRTIO_FEATURES,%s,%s\n' "${interface##*/}" "$(cat "$interface/device/features")"
done
echo 'NETWORK,METADATA,END'
[ "$mode" = serve ] || exit 0

printf 'NETWORK,SERVER,port=%s; connect from the Mac to the guest IPv4 address above\n' "$port"
echo 'NETWORK,SERVER,Ctrl-C stops the server; then run network-exercise stats'
exec iperf3 -s -4 -p "$port" --forceflush
