<!-- SPDX-FileCopyrightText: 2026 roolrz -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# HypeR system test cases

This repository owns full-system acceptance cases, Native test applications,
QEMU drivers, performance checks and immutable fixture recipes. Reusable harness
code belongs to [test-framework](https://github.com/HypeR-Proj/test-framework).
GitHub topic coordination belongs to [ci](https://github.com/HypeR-Proj/ci).

```sh
python3 test-framework/runner.py list
python3 test-framework/runner.py run native-apps-aarch64
make test MODULE=system-tests
```

`cases/*.json` declares an id, architectures, an argument-array command, optional
variants/environment, timeout and fixture name. `fixtures.json` declares fixture
builders. Add a case here; the manually started system workflow derives its job
matrix from this registry, so adding an ordinary case needs no CI workflow edit.
See [native-apps](cases/native-apps.json) for a minimal declaration.

RISC-V ordinary Native cases and VM acceptance have separate jobs. Lifecycle
stress runs remain 12 rounds each for one and four CPUs, in independent stress
shards; reducing serial runtime does not reduce stress coverage. Native images
are built once per architecture and shared where the case accepts that fixture.
Feature-specific kernel tests still build their own selected configuration.

`self-test.py` runs case-driver regression tests without booting a whole system.
Those tests belong to this repository's automatic module check. Runtime cases
run only after an explicit system-verification request. Execution logs and
per-case elapsed times are retained by the framework under OUT/results.

The Native service-lifecycle case also runs capability rendezvous through the
installed Rust SDK: receiver-local rights failure is distinct from matched
capacity/type/rights rejection, rejected MOVE keeps its source owner, and a
subsequent transfer on the same endpoint installs the intended object.

The case entry point is [ci/run.sh](ci/run.sh). See [suite ownership](ci/README.md)
for the responsibilities of the case families and `profiling/` for Perfetto validation helpers.

## Public admission recovery probe

The Native service lifecycle case exercises actual SDK receive-origin and MOVE
ownership. Its IPC probe interleaves malformed JSON, missing reply handles,
byte/handle capacity overflow, wrong object kinds and insufficient rights with successful
requests on the same listener. It also checks malformed stream admission followed
by a usable stream. Failed transfers retain sender ownership; malformed accepted
requests release their transferred reply handles. The fixture also injects malformed JSON into the real management observer and
component connector before successful management and publication requests.
These probes do not cover every service's independent wire protocol.

## Bounded service admission

The service lifecycle case runs the actual fs-backend and I/O broker admission
implementations with Native capability and byte channels. A silent filesystem
client cannot block a healthy request; eight pending commands reject overload,
expire and return capacity. Eight incomplete I/O memory handshakes still permit
an observer, and cancellation and listener shutdown return private endpoints.
This admission probe does not create a VM or publish a DMA mapping.

The same case exercises the running journald: more than eight retained query
endpoints work concurrently without per-client workers, pending endpoint
handoffs are bounded and expire, and existing query endpoints continue working
while handoff capacity is exhausted and after recovery.

## Request deadline transport probes

Native service acceptance verifies that a delayed admission followed by a delayed
reply cannot renew a caller budget. A non-reading client with a full reply queue
expires, and a later healthy request succeeds on the same listener. Expired
request envelopes release reply ownership before application dispatch. Invalid
peer framing is reported separately from local encoding failures. SDK probes
also verify that already-expired byte sends/receives leave queues unchanged.

The Native service probe's `publication` mode exercises real RPC owners around
the component manager's production command application function: queued close
and expiry must not change intent; reply loss after acceptance retains the
revision/generation and Stopping until explicit retirement. It also uses the
filesystem admission worker and publication guard with a closed queued request
followed by a healthy client. `make test-services` requires its success marker.

See [transport failure acceptance](docs/transport-failures.md) for the real-channel
coverage matrix, runnable entry points and the limits of each probe.
