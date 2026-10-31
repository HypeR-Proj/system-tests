<!--
SPDX-FileCopyrightText: 2026 roolrz
SPDX-License-Identifier: Apache-2.0
-->

# Native VM lifecycle smoke test

`make test-vm-smoke ARCH=riscv64` boots this privileged fixture as `/init` in a
separate initramfs. It is not part of the interactive system's app bundle.
The fixture needs the normal initial task, filesystem, console, resource-domain,
and VM-creation bootstrap capabilities. Its child executes the same `/init`
with a narrowed creation lease and rendezvous endpoint.

The position-independent guest in `src/guest.S` is assembled into the fixture
and copied into a fresh RAM VMO. Cases cover bootstrap register arguments,
administrative stop of a CPU-bound guest, SBI and direct Sstc timers, WFI with
global interrupts disabled, FP/GPR retention, software-interrupt clearing,
FIFO receive timeout and PLIC completion, fatal guest memory access, VU trap
delegation, process-owner cancellation, and repeated VM retirement.

On AArch64, `make test-vm-smoke ARCH=aarch64` exercises userspace MMIO
completion and guest-memory ownership. Its guest also checks distributor
`ISACTIVER`/`ICACTIVER` readback, write-one semantics, and preservation of pending
state across deactivation. It also sets 32 SPIs active, deactivates them with
DIR in reverse order, and checks every intermediate active bitmap and retained
pending bit. GICv3 additionally checks PMR, CTLR and idle RPR, including the TC
compatibility trap on hardware without TDIR. Run with GICv2 and GICv3 to cover
both LR backends. A kernel built with `kernel-test,kernel-vgic-tc-test` forces the GICv3 TC
path even when QEMU advertises TDIR, so running the same fixture against the
production and TC-test images covers both trap policies.

Every blocking observation has a finite deadline. `VM-SMOKE: PASS` is emitted
only after all cases finish; failures include `VM-SMOKE: FAIL`. The owner-loss
case deliberately keeps the VM handle in the cancelled child and transfers
only a vCPU observer to its parent, so observation cannot keep the VM alive.
