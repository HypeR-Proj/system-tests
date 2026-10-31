<!--
SPDX-FileCopyrightText: 2026 roolrz
SPDX-License-Identifier: Apache-2.0
-->

# Native scheduler benchmark suite

The Native test application lives in [`tests/app/scheduler-bench`](../../app/scheduler-bench/).
See the [user manual](../../docs/scheduler-bench.md) for
deployment, metric definitions, capability requirements and hardware commands.
The implementation uses HypeR interfaces; no rt-tests source is incorporated.

Run functional acceptance, including real worker processes and channels:

```sh
make test-scheduler-bench ARCH=aarch64 QEMU_CPUS=4
make test-scheduler-bench ARCH=aarch64 QEMU_CPUS=1
make app-test APP_TEST_PACKAGE=hyper-scheduler-bench APP_TEST_ARGS=--offline
python3 -B tests/performance/scheduler/test_report.py
python3 -B tests/performance/scheduler/test_capacity.py
```

The fixture contains a minimal I/O VM, a system EROFS and a disposable 512 MiB
FAT volume. It does not use an Alpine business VM or a user's persistent disk.
Each run gets a new board directory. Acceptance exercises all seven cases,
same/cross-CPU handoffs, CPU and memory pressure, catch-up, raw retention limits,
histogram overflow, bad configuration, offline affinity, missing executables,
existing output preservation, and denied scheduling/residency capabilities.
QEMU validates progress, protocol/lifecycle and statistical accounting, never a
latency performance threshold. The one-CPU run explicitly skips cross-CPU IPC.

For a longer manual TCG matrix, reuse a *disposable* prepared fixture and choose a
new output directory. The 512 MiB default accommodates the default four-CPU,
three-repeat reports, including their bounded raw samples. Older 64 MiB fixtures
are sufficient only for functional acceptance; recreate them using the command
above before a full manual run. `SCHED_BENCH_DATA_MIB` overrides the fixture size:

```sh
python3 -B tests/performance/scheduler/run.py \
  --qemu /path/to/qemu-system-aarch64 --cpus 4 \
  --image /path/to/hyper.img --initramfs /path/to/bootstrap.cpio \
  --disk /path/to/disposable/disk.img --board /path/to/deployment.json \
  --duration-ms 1000 --repeat 3 --output target/scheduler-manual-1
```

The default matrix runs a complete baseline suite and periodic tests with CPU
load, memory load, catch-up, and lower-weight clock observations. Before boot,
the runner validates GPT geometry and queries FAT free space via mtools.
A conservative bound covers every planned report, repetition, histogram, raw
sample and metadata, plus filesystem headroom. Insufficient space fails before
any measurement; it never silently shrinks samples. Larger CPU/repeat selections
may require a larger fixture. Existing files consume the available capacity.
The bound and observed free bytes are recorded with the host, QEMU version/command,
source revision/dirty state, artifact hashes and exact Native commands in
`manifest.json`. The disk is modified by the run and must not be shared with a
running VM. JSON exports travel through the filesystem after measurement, not
through the UART. Unmount fences writes; extraction happens after the owned
emulator exits. Serial and command-status logs remain available on failure.

Compact JSON reports retain every histogram and bounded timestamp sample;
indentation is omitted from machine files, while `--json` console output remains
formatted for reading.
`summary.csv` keeps individual series/repetitions, CPU placement, actual
scheduling, sample/overrun counts and latency statistics. An empty percentile
means its rank fell outside the histogram, not zero latency. Do not average
percentiles across repetitions; compare each distribution or merge compatible
histograms weighted by sample count. Use the same image configuration, CPU list,
interval, workload, warmup, histogram geometry and actual scheduling when
comparing revisions. Keep profiling/tracing and compiler activity disabled for
baseline measurements. Manual runs refuse CI; `--functional` is the only CI mode.

For board-produced files, validate/export without QEMU:

```sh
python3 -B tests/performance/scheduler/report.py baseline.json candidate.json \
  --csv scheduler-comparison.csv
```

Hardware tail latency needs repeated long runs under representative Native and
VM load. TCG results and a short observed maximum do not establish a real-time
bound or predict Pi 5 latency.
