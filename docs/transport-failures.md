<!-- SPDX-FileCopyrightText: 2026 roolrz -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Transport failure acceptance

Failure probes exercise the production Native channel implementation and the
installed SDK. Assertions cover ownership and subsequent progress, rather than
only checking that a process survives. Host tests retain pure parser, state and
media fault-injection coverage; they do not substitute for these transport cases.

## Service lifecycle case

Run from the repo-managed workspace root with the development service probes
selected (an empty `SYSTEM_CONFIG` uses the product defaults):

```sh
make test-services ARCH=aarch64 QEMU_CPUS=4 SYSTEM_CONFIG=
```

The `native-service-lifecycle` CI case also runs actual process retirement. The
[QEMU driver](../qemu/verify-services.py) requires success markers from each
configured probe and checks managed outcomes through the running service graph.

| Boundary | Production path and required outcome | Probe |
| --- | --- | --- |
| Rejected capability transfer | Receiver-local denial differs from matched-transfer rejection; failed MOVE retains the sender handle and publishes no destination; a later valid MOVE works | [Receive origin](../app/service-probe/src/rendezvous.rs) |
| Malformed service request | Oversized/malformed requests release received resources; a valid request succeeds on the same listener | [Admission recovery](../app/service-probe/src/admission.rs) |
| Silent filesystem client | A healthy client progresses while another stays silent; eight pending admissions refuse overload, expire and return capacity | [Bounded admission](../app/service-probe/src/bounded_admission.rs) |
| Incomplete I/O handshake | Observer traffic progresses at handshake capacity; cancellation returns a slot; explicit owner destruction closes pending peers | [Bounded admission](../app/service-probe/src/bounded_admission.rs) |
| Public I/O endpoint closure | Already-transferred private handshakes stay alive, and their completion precedes terminal EOF | [Public-closure regression](../app/service-probe/src/bounded_admission.rs) |
| Journal handoff capacity | Retained idle query endpoints do not consume handoff workers; existing queries work during overload and after expiry | [Running journal probe](../app/service-probe/src/bounded_admission.rs) |
| Absolute request budget | Delayed admission and reply share one deadline; expired byte operations leave queues unchanged | [Deadline probes](../app/service-probe/src/request_deadlines.rs) |
| Reply backpressure | A full, unread return queue expires; the next healthy request completes on the same listener | [Slow reply probe](../app/service-probe/src/request_deadlines.rs) |
| Cancellation before publication | Queued close/expiry leaves lifecycle intent unchanged; a canceled filesystem request fails the production publication guard | [Publication probes](../app/service-probe/src/publication.rs) |
| Reply loss after publication | Applied revision/generation remains inspectable; accepted stop retains its process until explicit retirement | [Publication probes](../app/service-probe/src/publication.rs) |

The publication probe uses real RPC owners and the component manager's production
command-application function with a controlled lifecycle instance. Its synthetic
process identifier is not evidence of hardware retirement. The separate process
retirement case verifies the running kernel/process boundary. The I/O admission
probe likewise does not install a VM or map DMA; the board-broker case exercises
those real service paths.

## Partial success and retained resources

```sh
make test-filesystem-failure ARCH=aarch64 QEMU_CPUS=4
make -C kernel test-qemu ARCH=aarch64 QEMU_CPUS=4
```

[Partial-write acceptance](../app/fs-failure/src/partial_write.rs) verifies short
writes, append progress and late storage errors through the filesystem RPC and
VFS path. The format-owned FAT tests separately inject allocation, buffering and
completion failures. Accepted bytes and durable bytes are different contracts;
a failure cannot be interpreted as proof that nothing was written.

[Writeback-pressure acceptance](../app/fs-failure/src/writeback_pressure.rs)
writes a pre-sized 7 MiB file against a worker with a one-hour periodic deadline.
It waits for a backend write before any explicit synchronization, checks bounded
512 KiB batches and coherent reads during partial drain, then verifies that an
explicit volume fence submits every accepted page. The fake backend retains a
page bitmap rather than a second full-file cache. This is a correctness test,
not a storage throughput or latency measurement.

The kernel's
[resource lifetime tests](https://github.com/HypeR-Proj/kernel/blob/main/tests/kernel/resource_lifetime.rs)
use independent resource domains and tiny DMA limits. A retained alias keeps its
charge, final release returns it after scheduler quiescence, a second domain
keeps its independent budget, and failed preparation rolls reservations back.
The kernel channel-service tests cover rejected transfer rollback and handle
counts; object tests cover subscription ownership through notification delivery.

These checks establish aggregate bounds and tested recovery paths. They do not
promise per-client fairness, exhaustive fault coverage, physical DMA/cache
coherence, or that timeout cancels a previously accepted operation. New protocols
must add their own ownership and healthy-client progress assertions here and in
the owning module; a success marker alone is not an acceptance criterion.
