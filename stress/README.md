<!--
SPDX-FileCopyrightText: 2026 roolrz
SPDX-License-Identifier: Apache-2.0
-->

# Lifecycle fuzz and stress

Two complementary correctness workloads target handle reuse and process teardown.
Neither is a throughput benchmark. The default CI budget is fixed; longer runs
are explicit local commands. Test applications are development-profile artifacts,
not part of ordinary system or bootstrap images.

## Stateful handle fuzzing

```sh
cargo test --manifest-path kernel/tests/host/Cargo.toml seeded_handle_transactions
mkdir -p target
HYPER_FUZZ_SEED=1000 HYPER_FUZZ_CASES=10000 HYPER_FUZZ_STEPS=1024 \
  cargo test --release --manifest-path kernel/tests/host/Cargo.toml \
  seeded_handle_transactions -- --nocapture > target/handle-fuzz.log 2>&1
```

The default is 32 seeds of 256 operations. Two production `HandleTable`s execute
batch creation/abort, duplication and rights attenuation, close/reuse, transfer
claim/rollback/commit, and empty-page reclamation. After every operation an
independent live-owner model checks object identity, rights, stale handles,
diagnostic enumeration, free-list integrity and exactly-once zero-owner callbacks.
Batch sizes straddle allocation boundaries; mixed valid/stale transfer batches
must roll back completely, and rejected operations must preserve
the model. Production capability and object code are compiled directly into the
existing host harness. Its accounting and signal stubs do **not** validate Process
accounting, actual channel cancellation or scheduler behavior.

This is deterministic, generation-based stateful fuzzing, not coverage-guided
libFuzzer or exhaustive interleaving exploration. A failure prints its seed and
last 32 operations. Replay with `HYPER_FUZZ_SEED=N HYPER_FUZZ_CASES=1` and the
same `HYPER_FUZZ_STEPS`. Seeds print before execution, including for a fatal
kernel invariant. Long campaigns should retain stdout/stderr as above. Cases are
bounded to 1,000,000 and each sequence to 4,096 steps.

## Native concurrency stress

```sh
# Build once and exercise both one and four CPUs. CI also runs this on RISC-V.
sh tests/ci/run.sh native-stress
CI_ARCH=riscv64 sh tests/ci/run.sh native-stress

# Longer run; use a distinct output name to preserve previous evidence.
make test-lifecycle-stress ARCH=aarch64 QEMU_CPUS=4 \
  STRESS_SEED=1000 STRESS_ROUNDS=240 \
  STRESS_LOG=target/app/aarch64/lifecycle-stress-long.log
```

`/bin/lifecycle-stress SEED ROUNDS` can also run in a development image. Each
round launches an actual child, waits for two handle-churn threads to finish
their first allocation/close wave, and exchanges capabilities across processes.
A third child thread blocks on a byte channel; a fourth receives transferred
handles and returns sequence-tagged echoes. The parent stops the process while
syscalls and transfer attempts remain active. Every third seed instead requests
cooperative worker shutdown and normal process exit. Rounds verify termination
reasons, failed MOVE ownership, received payloads and peer closure. The next
round must work in the same boot. The test has no crash-retention privilege.
The rendezvous ABI may return `FAULT` when receiver admission closes after
output copy but before commit. The test accepts that only after requesting
shutdown, checks that the MOVE source is still valid, and requires peer closure.
An unexpected `FAULT` during ordinary traffic remains a failure.

Default CI runs 12 rounds on one CPU and 12 on four CPUs per architecture. The
two architecture jobs reuse the ordinary immutable Native fixture instead of
rebuilding it. Manual campaigns accept up to 10,000 rounds, captured in batches
of 12. Each batch has a 240-second host deadline; kernel waits also have finite
deadlines. A timeout is a failure, never an automatic workload retry.

The shared command-result protocol checks exit status and exact captured output;
the oracle requires every round, nonzero successful transfers and the final
summary. Artifacts include raw serial, command output/status, failing command,
seed, round count, image SHA-256 hashes and QEMU configuration. On failure the runner attempts `ps --threads`,
`free` and recent journal records without replacing the original error. Seed
replay selects the same workload and termination mode; host scheduling and the
exact race interleaving are not reproducible from the seed alone.

These tests detect ownership, progress and endpoint-lifetime regressions. They
do not assert that global free memory immediately returns to an exact baseline:
page caches, allocator caches and asynchronous reaping have separate lifetimes.
They also do not prove DMA safety or all hardware memory orderings. Existing
Loom, process-retirement and service-failure suites remain complementary; see
[kernel validation](https://github.com/HypeR-Proj/kernel/blob/main/tests/VALIDATION.md).
