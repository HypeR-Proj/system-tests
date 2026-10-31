<!--
SPDX-FileCopyrightText: 2026 roolrz
SPDX-License-Identifier: Apache-2.0
-->

# CI test policy

`tests/ci/run.sh` defines system runtime acceptance. The manual
`System verification` workflow discovers its case jobs from `cases/*.json`, with
separate fixture construction, candidate resolution and result publication. It boots Native userspace, exercises services and storage,
and verifies guest/I/O VM integration. It does not rerun module checks.

Legacy suite names remain as forwarding shims to their owning module entrypoints
so workflows deployed before this split can validate its rollout. They are absent
from the new system matrix and contain no duplicate test implementations.

| Owner | Automatic PR module checks |
| --- | --- |
| kernel | Quality, scripts, feature configurations, AArch64 production/compact images, self-test + QEMU, RISC-V QEMU and x86-64 images |
| toolchain | AArch64/RISC-V SDK assembly, exported interface checks and installed-SDK consumers |
| build | Workspace/Cargo boundaries and build/deployment/package script tests, including flamegraph export |
| manifest | Source checkout layout and product descriptions |
| terminal-service | Linux terminal-formatting reference and service unit tests |
| ci | Topic coordinator, permissions, candidate snapshots and result gates; no system boot |
| test-framework | Session, protocol, timeout and report unit tests; no system boot |
| system-tests | Case drivers, fixtures, probes and assertion unit tests; no system boot |
| ABI, libraries and applications | Their declared component unit tests; Native package formatting is scoped to the owner |

The shared module runner checks only that repository's SPDX headers and shell
scripts. Python-only infrastructure declares `test_requires_rust: false` to avoid
installing an unused Rust toolchain. Kernel and SDK shards aggregate into the
required `module` check; a failed or cancelled shard cannot pass that gate.
The redundant `aarch64-build` aggregate is available locally in the kernel but
is not an extra workflow job. Its self-test image is built and validated in the
kernel's QEMU shard before booting it.

Run `python3 project.py test REPOSITORY` from the repo-managed workspace for the
module boundary. The source-oriented `sdk-test` and `app-test` aggregates remain
local helpers; CI relies on the owning modules rather than repeating these in
every system run. SDK consumer tests intentionally use the installed SDK.
Each repository's separate documentation workflow validates its own site and
publishes after changes merge into `main`.

| Suite | Required contract |
| --- | --- |
| `native` | Serial local aggregate of all AArch64 Native suites below |
| `native-gicv3` | Serial aggregate of the GICv3 boot, interactive, failure and VM suites |
| `native-fixture` | Build and package the ordinary Native kernel/initramfs pair; `CI_ARCH` selects AArch64 (default) or RISC-V |
| `native-boot` | Native boot and application startup on one/four CPUs |
| `native-console` | Paced input, pressure, shell restart and guest console |
| `native-terminal` | Terminal modes, transformations and flow control |
| `native-apps` | Native command-line applications plus editor/pager terminal interaction |
| `native-trace` | One/four-CPU disk-backed trace capture, authority, filtering, expiry, loss, nested IPC/remote filesystem flows, and raw artifact extraction after unmount/QEMU exit |
| `native-scheduler` | One/four-CPU scheduler benchmark correctness, same/cross-CPU IPC, loaded periodic wakeups, statistical accounting and capability failures; no latency thresholds |
| `native-pmu` | Opt-in PMUv3 overflow sampling on every CPU, bounded capture and exact-ELF flame-graph export on GICv2/GICv3 |
| `native-shell` | Shell language, editing, completion and redirection |
| `native-services` | Service readiness, scoped control, captured stdio, query and restart limits; bounded postmortem retention, terminal disconnect, early progress, failed-console recovery, runtime failure reporting and boot-profile selection/adoption |
| `native-fleet` | Fleet configuration admission and error handling |
| `native-storage-recovery` | Storage readiness and handoff failure recovery |
| `native-filesystem` | Filesystem RPC and provider failure |
| `native-runtime-crash` | Guest runtime failure and isolation in both fleet orders; console handoff also injects 250 ms receiver delay |
| `native-vm` | VM smoke on both GIC backends and forced GICv3 common-register traps |
| `native-gicv2` | GICv2 Native boot on one/four CPUs, console and runtime-crash recovery |
| `native-smp-stack` | Serial aggregate of the three SMP suites and `native-retirement-stack` |
| `native-smp-gicv3` | Guest SMP on GICv3 with four host CPUs |
| `native-smp-gicv2` | Serial aggregate of the following two GICv2 suites |
| `native-smp-gicv2-guest` | Guest SMP on GICv2 with four host CPUs |
| `native-smp-gicv2-retirement` | GICv2 SMP topology with abrupt runtime-exit retirement |
| `native-smp-overcommit` | Guest SMP with one host CPU |
| `native-retirement-stack` | Power-crash retirement in dormant/pending/powered-off states and stack limits |
| `native-stack-16k` | Native userspace boot on one/four CPUs with 16 KiB kernel thread stacks, independent RAMFS test root, root/supervision handoff, shell commands, static/dynamic std and application startup |
| `native-stress` | Seeded concurrent handle churn, cross-process MOVE and process stop/normal exit; 12 rounds each on one/four CPUs, supports `CI_ARCH=riscv64` |
| `riscv64-runtime` | Serial aggregate of the six ordinary Native probes with `CI_ARCH=riscv64`, plus `riscv64-vm` |
| `riscv64-vm` | RISC-V fleet configuration, VM authority on one/four harts, runtime-crash recovery and stack limits |
| `io-vm` | Pinned I/O appliance, cross-VM storage reset and standby acceptance on GICv2/GICv3 |
| `io-network` | Default physical/guest PCI storage and network lifecycle, with physical SMMUv3 translation |
| `io-network-mmio` | Dedicated physical/guest MMIO compatibility: storage, network, driver reset and VM restart |
| `board-storage` | Serial aggregate of the seven board suites below; storage/guest suites use 16 KiB stacks and stack watermarks, while product selection boots an uninstrumented kernel |
| `board-storage-media` | Async block queue admission, merge/completion/rights/close checks, configuration storage, cold restart and Native shell |
| `board-boot-system` | Independent root, supervision handoff and disk-independent/provider failure recovery |
| `board-alpine-rootfs` | Alpine ext4 root, write/sync/restart and checksums |
| `board-business` | Managed guest start/stop/restart and physical block operations |
| `board-broker` | Broker fairness, endpoint loss, active disks and Native storage |
| `board-userspace-device` | Native physical device backend through MMIO/IRQ capabilities |
| `board-product-config` | Minimal kernel and userspace product, omitted FAT/fleet components and complete bootstrap retirement |

System jobs have isolated filesystems and build their own runtime prerequisites.
Feature-specific fixtures keep their trap, stack and failure-injection variants.
Each board suite runs independently; the broker keeps its ordered lifecycle
scenario in one job. Logs are retained as separate diagnostic artifacts.

Guest SMP acceptance treats Linux soft-lockup watchdog reports as failures before
filtering ordinary guest printk output. After failure it attempts bounded,
read-only host observations: VM status, threads, CPU usage and recent journal
records. The serial log and `.log.commands` artifacts preserve this evidence;
collection neither restarts the guest nor replaces the original test failure.

Use `sh tests/ci/run.sh native-console` to build and run a single probe locally,
or set `CI_ARCH=riscv64` for the RISC-V fixture. `CI_NATIVE_FIXTURE` is optional;
when supplied, it must point to a validated artifact for the current commit and
architecture. An invalid artifact fails instead of silently rebuilding. The
`native`, `native-gicv3`, `native-gicv2`, `riscv64-runtime` and `board-storage`
aggregates remain serial for local reproduction. Do not run suites concurrently
in one checkout: feature variants and initramfs fixtures share output paths.

The kernel repository's architecture QEMU suites deliberately build with
`kernel-test,kernel-self-test` and use an empty initramfs. They do not select a guest.
The master `kernel-test` feature is absent from production defaults and does
not select a test by itself. Every test selector also requires this explicit
master gate, including production-boot fault and stack-audit fixtures.
The kernel module's `configurations` shard compiles an empty production feature
set, the master gate alone, each optional production capability and every
supported selector/master pair. It also compiles selectors without the master
in the binary, core and HAL crates and requires the intended admission error.
An unrelated compiler failure cannot satisfy these negative cases. Cargo's
resolved default graph is checked for accidental test activation.
Every feature declared by the kernel, core and HAL crates must also be classified
as production functionality or an explicitly covered test selector.
The build module validates product allowlists, child-program requirements,
generated service omissions and removal of stale installation artifacts.
Production images instead mount the firmware initramfs and start Native
`/init`, then mount an independent system volume before the root and supervision
handoff. The separate `native` suite assembles a synthetic no-device fixture
from the in-tree SDK and application sources. Its test-only init copies a formal
payload into a distinct RAMFS mount and enters the ordinary stage-1 supervisor;
formal executables are not mapped from the original archive. This exercises the
root transition, service handoff and application contracts without requiring a
physical block provider on every architecture. It does not validate GPT, EROFS,
storage-provider replacement, or product boot latency and memory consumption.
The separate `board-storage` suite verifies the real independent disk root and
bootstrap retirement. The synthetic fixture is not a supported product layout;
ordinary board archives contain static stage-1 programs only.
The independent `native-stack-16k` shard runs this acceptance with the minimum
supported kernel thread stack on one and four CPUs. It requires the boot log to
confirm 16 KiB stacks and the shell to finish the application probes; reaching
kernel startup alone is insufficient. IRQ and emergency stacks retain their
defaults. Run `sh tests/ci/run.sh native-stack-16k` locally; its generated config
does not modify `kernel/.config`. CI retains the config and both serial logs.
The RISC-V Native image includes userspace VM provisioning. Its Native suite
checks Linux guest startup on one and four host harts and runtime-crash recovery;
this does not imply multi-vCPU RISC-V guest support.

Native acceptance reports each phase transition with elapsed times. Each phase
has a 90-second progress deadline (`QEMU_BOOT_TIMEOUT_SECONDS`), and the complete
multi-command run has a 300-second deadline (`QEMU_NATIVE_TIMEOUT_SECONDS`).
The workflow retains the raw Native console log alongside runtime-crash logs,
including on failure, so a stalled phase can be distinguished from exhaustion
of the overall test budget.
The separate guest SMP scenario allows a fixed 300 seconds for each guest boot
or reboot on shared TCG runners; ordinary commands retain a 90-second deadline.
On failure it prints the raw serial tail, including filtered guest boot records.
Native boot, application, fleet, runtime-crash, service and storage-recovery
command assertions use the result protocol described below. Interactive guest
console and terminal input probes use their own readiness markers.

## QEMU observations

PCI deployment, disk-backed trace capture, network and DMA containment jobs
run in Debian 13 (`trixie`)
containers, using the distribution QEMU package. They require QEMU 9.2 or newer
for SMMUv3 stage-2; Ubuntu 24.04's package lacks that model. CI does not build
QEMU. Package caches are separated by distribution and version. The dedicated
MMIO network matrix entry remains enabled, and the independent MMIO cross-VM
reset job also retains coverage on Ubuntu's older QEMU. No benchmark thresholds
are part of these jobs.

Network acceptance boots its two SMP guests one at a time to bound TCG startup
contention. Both remain running during payload checks, driver rebinds and peer
restart checks; guest boot retains a 300-second deadline.

Choose the oracle for the behavior being tested:

* `Session` owns QEMU, deadlines, raw serial archival and process cleanup.
  `await_text` consumes the interactive cursor. `observe` reads the archive with
  an independent cursor; use `mark()` before an action when old evidence must
  not satisfy a new assertion. A prompt is terminal output, not service readiness
  or proof that asynchronous logs have all arrived.
* `command.Shell` runs finite formal-shell commands once. Commands enter through
  bracketed paste to avoid per-character echo delays under concurrent kernel
  logs. It captures merged
  stdout/stderr in `/tmp`, then the development-only `/bin/test-report` exports
  numbered hex chunks and a footer carrying the command's status, byte length
  and FNV-1a checksum of **status and payload**. The saved status is bound to the
  command token and written inside the capture group: failed output/status
  writes cannot reuse a previous command's result. A fresh token identifies every
  command and a separate builtin receipt establishes return to the shell after
  the exporter has exited. Echo, stale prompts and unrelated logs cannot become
  command results. Bad transmission permits at most two re-reads of the saved
  result with fresh transfer tokens, within the original deadline; the command
  is never retried. Persistent corruption is an observation failure, not a
  successful test. Exported results are limited to 1 MiB; commands must bound
  their captured output and wait for their writers to finish. This checksum
  detects accidental corruption, not malicious tampering.
* `journal.Journal` queries bounded JSON pages through `journalctl --json`, pins
  the snapshot's upper cursor, and checks component, generation and stream.
  Wait for the expected records explicitly: process completion does not imply
  that journal ingestion has reached EOF. Retention drops or rejected sources
  invalidate completeness assertions; dropped serial mirror bytes do not.
  A stalled or regressing page cursor cannot complete a partial snapshot.
  Journal is a bounded diagnostic store, not an unlimited full-log archive.
* Raw terminal/editor/pager tests continue to exercise actual bytes and UI
  behavior. Redirecting those commands would change the thing under test.
  Bootstrap recovery cannot depend on a formal journal or test exporter. Require
  the recovery operation and namespace/lifecycle outcome; diagnostic prefixes
  and message bodies from separate writers need not be adjacent on the UART.

The result helper has no kernel/ABI hooks, special capabilities or bootstrap
dependencies and is absent from the normal `system` build profile. Test results
remain on the host beside each raw log in `.log.commands/`, with command text,
exit status, exact output bytes and any damaged transfers. CI uploads these
directories with serial logs on both success and failure. Host regression tests
inject split reads, interleaved logs, old prompts, corrupted status/payload,
missing/reordered chunks, source loss and delayed generations. They also execute
the actual Rust exporter against a host shell to check side effects are not
repeated by a result re-read.

Capture commands must wait for their output writers before returning; detached
background writers and shell replacement/exit belong in dedicated scenarios.
Session cleanup checks fatal diagnostics drained after the final assertion too.

Existing board/device-specific and early-kernel serial runners still require
their own device evidence; do not replace genuine device/terminal checks with a
journal banner. New ordinary command assertions should use the shared result
protocol instead of adding prompt counters or another subprocess/read loop.

The AArch64 QEMU matrix covers one and four CPUs with the VHE-capable `max`
model, plus constrained-memory and 42-bit compact-address-space cases. FEAT_VHE
is required; the CPU's Arm version is not sufficient. Successful boots require
canonical upper kernel/KASLR geometry and private lower Process roots. The UP
case also boots an unsupported `cortex-a72` and uses QMP register inspection to
prove it reaches the dedicated FEAT_VHE rejection loop before MMU setup.
Every case verifies kernel self-tests, guarded thread, IRQ and
emergency stacks, scheduler and sleeping synchronization, SMP admission,
the selected GICv2/GICv3 backend, host and guest timers, virtual system registers, PL011 RX, KASLR
geometry, allocator ownership statistics, and lazy guest demand paging.
The separate Native suite validates guest userspace and interactive console
input. The kernel matrix also requires Native dispatcher validation, bounded Channel
transaction tests, and the AArch64 VHE raw-code EL0 proof: repeated direct
`abi_query` and unsupported-call returns, scheduling and lifecycle calls, Event
creation/signal/wait, active COW copyout, blocked syscall migration with FP/TLS
preservation, contained breakpoint fault,
Process/Thread join, and acknowledged retirement.

The standalone AArch64 runner explicitly uses multithreaded TCG. With at least
three CPUs, its wait-arbitration test races two remote resolvers through Armed
and queued waits, then checks worker retirement; smaller configurations require
an explicit skip marker. Host `quality` additionally runs Loom scenarios
against production publication/admission mechanisms. See the commands and
coverage limits in [validation coverage](https://github.com/HypeR-Proj/kernel/blob/main/tests/VALIDATION.md).
These are correctness tests; storage/network performance benchmarks remain
manual and have no CI throughput or latency thresholds.

The kernel module's `quality` shard includes 32 deterministic handle-fuzz seeds, each with 256 operations
against production handle/object code. `native-stress` adds real concurrent
Native syscall/stop coverage using the shared fixture on one/four CPUs. It has
fixed CI round limits and preserves the seed and command results on failure.
Longer campaigns and exact replay commands are documented in
[lifecycle fuzz/stress](../stress/README.md); fuzz seeds do not reproduce exact
scheduler interleavings.

`make test-boot-system ARCH=aarch64` reproduces the split-root and recovery
part of `board-storage`. It creates an isolated disk, checks supervision handoff,
the final namespace, shell/tool capabilities, VM start/stop and writable/read-only
unmount/remount behavior, then boots the same archive without storage controllers
and requires the static recovery shell to remain usable. Normal boot also checks
that provider startup rows are not repeated, start/total durations retain two
decimal places, and retirement completes before the shell prompt. These display
checks reassemble interleaved kernel diagnostics before parsing ANSI output;
the `scripts` suite covers stack-audit insertion inside status rows and escape
sequences without hiding fatal diagnostics. A third boot uses
a copied bootstrap policy selecting an exiting init candidate; it verifies
namespace rollback, live storage services, and static recovery mount operations.
Two further boots use separate system-image copies missing the formal I/O runtime
or EROFS worker. They require failed replacement preparation to retain the original
providers and `/boot`, keep the formal shell available, and serve mount, write,
unmount and remount requests through the retained filesystem manager.
Three mounted-worker fault cases replace only the formal FAT executable with a
test provider that never writes media. Explicit process exit and a user CPU
exception must be observable in the journal and component/mount status, while
root listing still displays healthy entries and `/tmp` remains usable. A fault
after namespace detach must retain its diagnostic after the mount owner drops. The
fault provider is not built or packaged in the default system profile.
The fallback shell does not receive a formal loader or formal shell delegation;
the recovery cases do not launch a system shell through it. Separate synthetic
storage-failure acceptance includes formal-shell failure after handoff, which
must not reopen bootstrap recovery.
The disk and all serial logs stay under `target/board-tests/boot-system.*/`;
this test never modifies the developer's deployment disk.

Failed QEMU jobs retain their complete serial logs as CI artifacts. Guest
Linux inputs are checksum-pinned by `kernel/tools/guest` and cached only as CI
inputs; they are not included in the distributable kernel artifact.

Final bootstrap-stack sizes have Rust const assertions in each architecture's
layout owner. Image validation independently checks the linked initial stack's
symbol span. Both require at least 256 KiB for bounded, allocation-free firmware
discovery; neither depends on how a constant is spelled.

`check-license-headers.sh` requires every project-authored tracked text file to
carry SPDX copyright and Apache-2.0 identifiers. The complete license text and
Cargo-generated lockfiles are intentionally exempt.

The build repository's `scripts/check-workspace.py` parses Cargo manifests to enforce in-tree ABI ownership,
installed-SDK consumption and app-local sharing. It recognizes renamed packages,
workspace dependencies, target-specific tables and patches. Apps cannot declare
a direct `hyper-sys` dependency. Normal compilation resolves imports; comments
and documentation mentioning a crate are not dependency violations. The check
also reads Git index modes to reject submodules and obsolete component locks.

`kernel/tests/ci/hal-boundary.py` checks Cargo's resolved dependency direction
and compiles public/private HAL import probes. The negative probe must fail with
rustc E0603 at the intended import, with no unrelated compiler errors. Network
errors or a broken public interface cannot count as a successful privacy test.
There are no per-rule shell wrappers.

Ownership traits and consuming IRQ API signatures are checked against the real
types in `kernel/tests/kernel/type_contracts.rs` whenever both `kernel-test` and `kernel-self-test` are
compiled, including the AArch64, RISC-V and x86-64 build checks.

See [validation coverage](https://github.com/HypeR-Proj/kernel/blob/main/tests/VALIDATION.md) for subsystem
coverage and the limits of these checks. Do not add grep/sed assertions over
function bodies or `include_str!` tests over Rust implementations. Such checks
freeze a spelling or statement order without executing the invariant. Use a
compiler check, a behavioral test of production code, or an artifact/runtime
test appropriate to the property. Assembly/weak-memory ordering and unsafe
lifetime proofs still require review; a source pattern was never a substitute.
