<!--
SPDX-FileCopyrightText: 2026 roolrz
SPDX-License-Identifier: Apache-2.0
-->

# Raspberry Pi 5 storage qualification

Manual physical-board tests. Do not run these measurements in CI or substitute
QEMU results. The payload is exactly **1 GiB** per file. Ordinary measurements
write one pass per command; `stress` repeatedly uses the same 1 GiB extent until
an explicitly injected owner failure. There is no raw-device test mode.
The payload filename must be `storage-qual.bin`; use a regular file, never a
symlink or device node. Create commands replace that file. Use only the
dedicated, disposable qualification cards.

The comparison covers Native HypeR `/data`, the Alpine guest's ext4 root disk,
and a process on the existing Raspbian card. No new Raspbian image is needed.
Use the same Pi, power supply, cooling and SD slot. Two cards of
the same model are still different devices. HypeR uses FAT32; retain and record
Raspbian's existing filesystem, normally ext4. The result describes these two
deployments, not isolated hypervisor overhead or a controlled filesystem study.

For baseline/candidate comparisons, keep the published I/O VM, guest CPU
placement and test parameters fixed, record both build identities, and report
repeated runs with medians and ranges. Collect the
[network measurements](../network/README.md) separately from storage workloads.

## Build and artifacts

These payloads are opt-in. Default Make targets, board profiles and Native
`system`/`development` images do not compile or package them. Only the explicit
preparation script below adds them to a separate test image. Fault injection
requires the additional `--retirement-probe` option and is disabled otherwise.

From the repository root, with the ordinary AArch64 configuration and cached
published dependencies:

```sh
python3 -B tests/hardware/storage/prepare.py \
  --output target/board/rpi5-storage-qualification
```

The output contains `disk.img` for the **HypeR test card**, `storage-qual-linux`
to copy to existing 64-bit Linux, and `build.json` with hashes and source/build
identity. The Linux executable is static AArch64 and needs no libc installation.
Its ELF segments use 64 KiB alignment, supporting 4/16/64 KiB kernel pages,
including the [Pi 5 default 16 KiB kernel](https://www.raspberrypi.com/documentation/computers/linux_kernel.html).
Both executables link the exact same three workload objects. The HypeR image
derives from `boards/rpi5.json`, uses the Native `system` profile, enlarges its
configuration volume to 2 GiB, and adds Native `/bin/storage-qual` plus Alpine
`/usr/bin/storage-qual`.
Alpine receives the exact `storage-qual-linux` executable used on Raspbian;
its base rootfs cache, kernel and initramfs are unchanged. The network deployment
and published I/O VM pin are preserved. The ordinary
`target/board/rpi5-native/disk.img` is untouched.
Use `--payload-only` to build binaries with an already installed Native SDK.
The output directory must be new. No preparation command flashes a card or
runs a workload on the board. To also add guest network tools, continue with
the [network image preparation](../network/README.md#image-preparation).

Do not flash a HypeR test image onto the Raspbian card. Preserve `build.json` and the
full serial log for every run. Flashing and physical power control are manual.

## First round: writes and synchronization

Keep business VMs stopped and unrelated applications idle. Record the complete
HypeR boot log, including HypeR and I/O VM versions, then run:

```text
free --bytes
/bin/storage-qual write /data/storage-qual.bin 101
/bin/storage-qual overwrite /data/storage-qual.bin 102
/bin/storage-qual sync4k /data/storage-qual.bin 102
free --bytes
```

On the existing Raspbian card, copy `storage-qual-linux` into the home directory.
Record the following before and after measurements; unavailable diagnostics
should be recorded as unavailable, not silently assumed equal:

```sh
uname -a
getconf LONG_BIT
getconf PAGESIZE
findmnt -T "$HOME" -o SOURCE,FSTYPE,OPTIONS
lsblk -o NAME,SIZE,MODEL,FSTYPE,MOUNTPOINTS
free -b
vcgencmd version
vcgencmd get_throttled
vcgencmd measure_temp
vcgencmd measure_clock arm
cat /sys/block/mmcblk0/device/name /sys/block/mmcblk0/device/cid
cat /sys/block/mmcblk0/queue/write_cache
```

The ELF requires an AArch64 Linux kernel (`uname -m` reports `aarch64`); a
32-bit userspace on that kernel does not require a 32-bit benchmark binary.
Check that the home filesystem is on the SD card and has at least 1.1 GiB free.
Then run, capturing stdout/serial on another computer:

```sh
chmod +x ./storage-qual-linux
./storage-qual-linux write "$HOME/storage-qual.bin" 101
./storage-qual-linux overwrite "$HOME/storage-qual.bin" 102
./storage-qual-linux sync4k "$HOME/storage-qual.bin" 102
```

Do not write the capture log to the measured filesystem while timing. SSH to
Linux with host-side output capture is fine; the HypeR Native shell uses serial.
Start with one run per OS. For repeatability, alternate boots of the two cards
and collect three runs each, rather than continuously warming only one OS.
Keep the same firmware, clocks and cooling settings; record differences in
admitted RAM, CPU governor and background activity. HypeR does not currently
export the same Pi thermal/throttling diagnostics as Linux.

`write` includes file growth and allocation, with open/truncate outside timing.
`overwrite` requires an existing full extent. Both use 128 KiB requests, then
one full-volume synchronization. `TOTAL_NS` includes deterministic payload
generation, writes and synchronization; `SYNC_NS` separately reports the final
sync. There is no console output or readback inside that interval. Every word
depends on its offset, run ID and epoch, and all 1 GiB are checked afterward.
This immediate check can hit caches; it is not independent media verification.
For a cold readback, reboot and run `check` with the last writer's run ID.

`sync4k` performs 128 deterministic 4 KiB overwrites, synchronizing after each.
It records all operation latencies after timing. It preserves the verified
pattern; it is a latency test, not a changed-data durability proof. Application
queue depth is one. The Linux adapter uses `syncfs` on the file's filesystem;
HypeR's FAT sync also commits the whole mounted volume. These are **volume-sync
latencies**, not Linux per-file `fsync` latency or O_DIRECT/raw-device results.
Other writes on Raspbian's root filesystem can therefore affect its results.

Summarize externally captured logs on the development machine:

```sh
python3 -B tests/hardware/storage/results.py compare \
  --hyper hyper-writes.log --linux linux-writes.log > storage-comparison.json
```

The summary retains individual measurements, uses medians for repeated runs,
and reports HypeR/Linux ratios only for modes present in both logs. Missing
completion, verification or timing records are errors. Preserve metadata beside
the JSON; do not claim equivalence of cards or filesystems from the ratio.

## Alpine guest comparison

The qualification image also contains the Linux payload on Alpine's persistent
ext4 root disk. Start the guest after completing the Native measurements:

```text
vmm start alpine
vmm console alpine
```

Inside Alpine, record its configuration and run the same workload:

```sh
uname -a
cat /proc/mounts
free -m
df -h /root
/usr/bin/storage-qual write /root/storage-qual.bin 101
/usr/bin/storage-qual overwrite /root/storage-qual.bin 102
/usr/bin/storage-qual sync4k /root/storage-qual.bin 102
```

Require `/` to be the writable ext4 filesystem on `/dev/sda`, with at least
1.1 GiB free. Do not use `/tmp` or an initramfs: that would measure RAM instead
of the SD-backed guest disk. Keep the full guest output separate from Native
and Raspbian logs. Ctrl-] opens the console detach menu; stop Alpine before
repeating Native measurements.

The board's existing guest settings remain 256 MiB RAM and two vCPUs. These
results include the guest filesystem and I/O VM backend; memory limits, kernel
versions and filesystem options differ from native Raspbian. All three use
the same 1 GiB payload and volume-sync timing convention. Report guest results
as a separate third system, never mixed into the Native measurement series.
The existing two-log summarizer can compare the guest against Raspbian by
passing the guest log to `--hyper`; its `hyper` JSON key then denotes that
guest measurement series. Preserve that label distinction when presenting it.

## Power-cut durability

This procedure and the physical retirement/stress procedure below are deferred
[qualification TODOs](https://hyper-proj.github.io/HypeR/docs/project/roadmap.html#deferred-hardware-qualification).
They have not been performed in the initial survey.

Use the ordinary qualification image, not the owner-loss image below. Capture
the entire serial stream on an independently powered computer. A file on the
tested card cannot certify which writes were acknowledged before power loss.
Use a new nonzero run ID for each trial; never concatenate different trials
under one ID. Commands below are for HypeR; use the Linux executable and its
home-directory path for the same protocol on Raspbian.

```text
/bin/storage-qual prepare /data/storage-qual.bin 201
/bin/storage-qual durable /data/storage-qual.bin 201
```

`prepare` creates, syncs and verifies the full 1 GiB epoch-0 file. Wait for
`PREPARED` and `END`. `durable` verifies that baseline, then overwrites successive
1 MiB ranges with epoch-1 data. Each range is synchronized before emitting its
`ACK`; acknowledged ranges are never overwritten again. A 100 ms pause after
each ACK permits manual power cuts. Remove **all** board power during this
command, including any USB power source; a reboot command or process kill is
not a power cut. Start with cuts after approximately 5, 20 and 50 ACKs; include
cuts during a write and immediately after a host-received ACK. Record the cut
method and delay. Three trials are exploratory evidence, not a failure rate.

On the development machine, extract only complete externally received ACKs:

```sh
python3 -B tests/hardware/storage/results.py oracle power-cut.log --run-id 201
```

After a cold boot, run recovery with the returned `ack_mib`, for example:

```text
/bin/storage-qual recover /data/storage-qual.bin 201 20
```

Every byte in the externally acknowledged prefix must match. Unacknowledged
tail chunks are classified as old, new or torn; their presence must not hide
lost acknowledged data. Keep recovery logs and boot/fsck messages. Missing
files, unmountable volumes, truncated files, wrong IDs and `LOST_ACK` are failed
trials. Do not run `prepare` again or repair a failed card before preserving the
evidence. Linux boot-time journal replay/fsck is part of the observed recovery;
record it, and do not claim a pre-repair filesystem check was performed.

This protocol tests durability of acknowledged data in a preallocated file.
It does **not** prove atomic FAT metadata updates, create/rename transactions,
all possible cut timings, or durability on other SD models. FAT is not journaled.
Passing results qualify only the documented card/firmware/software combination.
The Linux block layer's [flush/FUA contract](https://docs.kernel.org/block/writeback_cache_control.html)
also depends on the device and driver accurately reporting volatile-cache behavior.

## Physical DMA retirement and owner loss

Current Pi SDHCI and PCI assignments cannot certify hardware DMA drain. Active
I/O VM retirement must retain the VM, physical claims and all imported memory
until a host reboot. A successful business-VM `RELEASE_MEMORY` exchange is a
different path: its live I/O VM drains the backend before unmapping that
business VM. Neither throughput nor an absence of visible corruption proves
that physical DMA stopped.

Build an explicitly separate destructive owner-loss image:

```sh
python3 -B tests/hardware/storage/prepare.py --retirement-probe \
  --output target/board/rpi5-storage-retirement
```

Only this image builds `io-runtime` with `physical-retirement-probe`, in a private
Cargo output directory. Its applications and Rust shared libraries are built and
staged together in private `probe-apps/` output. It preserves the published Linux appliance. Sixty
seconds after storage readiness, a test thread exits the entire Native I/O
runtime with status 99, without cooperative Linux/device shutdown. It is not a
performance image. The test does not reset controllers or weaken quarantine.

Boot this image with full serial capture and immediately run:

```text
free --bytes
ps
handle
vmm start alpine
/bin/storage-qual stress /data/storage-qual.bin 301
```

After the `DMA-QUAL` termination marker and the failed I/O command, collect:

```text
free --bytes
ps
handle
vmm list
dmesg
```

Repeat observations after at least 30 seconds. Require the expected status-99
owner exit, `DeviceQuarantined` retirement diagnostics, continued Native shell
availability, and retained physical-device/VM owners and guest memory. A
physical VM retirement success or apparent return of its backing to free
memory is a failure requiring investigation. Cache eviction can change total
free memory: correlate guest-owner accounting and object identities, not just
the free total. Record whether Alpine was still admitted when the owner failed.
If it was not, the trial does not cover imported business-VM RAM.

The timer deliberately targets sustained I/O, but is not a hardware trace that
proves a DMA transaction was in flight at the exact exit instruction. Likewise,
accounting and object snapshots support the retained-owner code audit; they do
not directly observe every physical page. Label this result **owner-loss
quarantine qualification**. Successful physical reset/drain and safe page reuse
remain unimplemented and unqualified, even if every trial passes. Do not mark
the roadmap's physical DMA retirement TODO complete on this evidence. Raspbian does not
have this separate I/O VM owner, so there is no meaningful Linux speed ratio
for the fault case.

For successful ordinary guest retirement on the production image, exercise SD
writes inside Alpine, then stop/start it repeatedly and capture every backend
release acknowledgement plus guest-owner accounting. Keep this distinct from
the fatal physical-owner trial. Interrupted writes need not be durable without
their own completed sync.

## Local harness checks

```sh
python3 -B tests/hardware/storage/test-storage.py
```

These host checks use a 2 MiB file and UBSan to exercise the oracle, corruption
detection and incomplete-log rejection. They are not physical measurements and
do not change the production 1 GiB payload. The qualification commands and
timing thresholds are not added to CI.
