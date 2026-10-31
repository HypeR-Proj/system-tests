<!--
SPDX-FileCopyrightText: 2026 roolrz
SPDX-License-Identifier: Apache-2.0
-->

# Pi 5 guest network exercise

Manual data collection only. No QEMU measurements or CI performance thresholds.
Keep storage tests stopped while collecting network throughput.
The default build and board images do not download, compile or package these
tools. The explicit preparation command below adds them to a separate image;
neither it nor the normal guest boot starts a performance workload.

The Alpine guest is the iperf3 server. The Mac initiates every TCP connection,
including reverse-direction tests; neither a listener on the Mac nor a guest
connection to the Mac is required. Permit Mac-to-guest TCP port 5201 through the
test network's firewall. Use the guest's IPv4 address, not the I/O VM's address.

## Image preparation

First build the ordinary [storage qualification image](../storage/README.md#build-and-artifacts)
at `target/board/rpi5-storage-qualification`, without `--retirement-probe`.
Reuse its kernel, bootstrap, I/O VM and Alpine kernel. Download the package and
upstream license/notices selected by [iperf3.lock.json](iperf3.lock.json):

```sh
mkdir -p target/hardware-tools
curl --fail --location \
  https://dl-cdn.alpinelinux.org/alpine/v3.23/main/aarch64/iperf3-3.19.1-r1.apk \
  -o target/hardware-tools/iperf3-3.19.1-r1.apk
curl --fail --location \
  https://raw.githubusercontent.com/esnet/iperf/3.19.1/LICENSE \
  -o target/hardware-tools/iperf3-3.19.1-LICENSE
python3 -B tests/hardware/network/prepare.py \
  --base target/board/rpi5-storage-qualification \
  --package target/hardware-tools/iperf3-3.19.1-r1.apk \
  --license target/hardware-tools/iperf3-3.19.1-LICENSE \
  --output target/board/rpi5-io-qualification
```

The output directory must be new. The builder verifies base artifact, package
and license hashes. It adds the matching
Alpine v3.23 AArch64 package beneath `/opt/hyper-network`, with a wrapper at
`/usr/bin/iperf3`. Its musl and OpenSSL dependencies already exist in Alpine;
system libraries and the APK database are unchanged. The builder checks the
Alpine release and required loader/library paths. Package metadata, full
upstream license/notices and the provenance lock are retained under
`/opt/hyper-network`; the APK and license also accompany the output image.
The image also retains Native and guest
`storage-qual` with the same 1 GiB workload previously used on Raspbian.
Flash `target/board/rpi5-io-qualification/disk.img` to the disposable HypeR card.
The base image, normal board image and cached Alpine rootfs remain unchanged.

## Run on the board

From the Native shell:

```text
vmm start alpine
vmm console alpine
```

Inside Alpine:

```sh
network-exercise
```

This prints kernel/tool versions, interface addresses, routes and counters,
then runs the server in the foreground. Record the guest IPv4 address. It does
not change DHCP, reset the interface, or start automatically at boot.
For virtio interfaces it also records negotiated feature bits as
`NETWORK,VIRTIO_FEATURES,INTERFACE,BITS`. Linux lists bit 0 first; TX checksum,
TCPv4 segmentation and TCPv6 segmentation are bits 0, 11 and 12 respectively.
Keep this record with both image identities when comparing offload candidates.

## Collect on the Mac

Install iperf3 on the development Mac (for example, `brew install iperf3`).
Replace the example address
with the guest address printed above:

```sh
python3 -B tests/hardware/network/exercise.py 192.0.2.10 \
  --output target/hardware-results/alpine-network-1
```

The four sequential tests measure guest receive and transmit with one and four
TCP streams. Each measures 15 seconds after a 2-second warm-up, taking about
one minute in total. `guest-rx` is Mac to guest; `guest-tx` is guest to Mac.
Raw iperf3 JSON, server output, errors, commands and a summary are saved on the
Mac. Receiver throughput uses decimal Mbit/s; sender retransmissions and CPU
utilization remain available in the raw JSON. Interrupted/failed tests retain
evidence and fail the collection command, without claiming a passing result.

Afterward press Ctrl-C inside Alpine and run `network-exercise stats` to capture
the final counters. Retain both serial output and the Mac result directory.
These are TCP transfer/throughput measurements, not an application payload hash
verification or an exhaustive networking qualification. Record whether the Mac
uses Ethernet or Wi-Fi; switches, Wi-Fi, filtering and the peer can limit the
result. No minimum throughput is treated as a correctness threshold.

For a native Raspbian network baseline, run its iperf3 server and repeat the same
Mac collection command against its address, into a separate result directory.
Record that server's version and keep the peer/link/test parameters identical.

On Raspbian:

```sh
sudo apt install iperf3
iperf3 -s -4 -p 5201 --forceflush
```

On the Mac:

```sh
python3 -B tests/hardware/network/exercise.py RASPBIAN_IP --label raspbian \
  --output target/hardware-results/raspbian-network-1
```

Here `raspbian-rx` is Mac to Raspbian and `raspbian-tx` is Raspbian to Mac.

## Local harness checks

```sh
python3 -B tests/hardware/network/test-network.py
```

These checks use temporary archives and simulated iperf3 output to verify
packaging, direction labels, receiver metrics and failure evidence. They do
not connect to a server, measure throughput or run as part of QEMU CI.
