<!--
SPDX-FileCopyrightText: 2026 roolrz
SPDX-License-Identifier: Apache-2.0
-->

# Scheduler benchmarks

`sched-bench` measures application-visible scheduling latency and contention on
HypeR Native. It is a development tool, excluded from ordinary product images.
Enable `SYSTEM_APP_SCHED_BENCH` in the product menu, or add
`APP_EXTRA_BINS=hyper-sched-bench` to the build. It installs `/bin/sched-bench`
in the formal system, never bootstrap. Keep the same selection when packaging
the board image. The test fixture is available through
`make test-scheduler-bench ARCH=aarch64 QEMU_CPUS=4`.

The periodic measurement follows the absolute-deadline method used by
[cyclictest](https://wiki.linuxfoundation.org/realtime/documentation/howto/tools/cyclictest/start).
This is an independent HypeR implementation, without Linux signal, pthread,
POSIX scheduler or ioctl compatibility requirements.

## Cases and metrics

| Command | What is timed | Interpretation |
| --- | --- | --- |
| `clock` | Two consecutive monotonic observations | Measurement floor; not hardware timer resolution. |
| `cyclic` | Actual wake time minus an absolute periodic deadline | Timer, wakeup and scheduling lateness, including competing work. |
| `yield` | Monotonic time before/after `yield_now` | Yield path and competition; a yield need not switch threads. |
| `atomic` | Atomic wait/wake ping-pong between two pre-created threads | Direct one-way receive latency and full round trip, measured separately. |
| `channel` | Ping-pong over a Native byte channel between two processes | IPC, wakeup and scheduling; same CPU or cross CPU. |
| `fairness` | Identical integer work completed by competing threads | Per-thread progress and Jain index; not CPU utilization or policy entitlement. |
| `spawn` | Thread creation request until child entry | Runtime allocation, kernel creation and first dispatch; join is outside each sample. |
| `suite` | All of the above, plus cross-CPU channels when selected | A common report; cases execute sequentially. |

One-way measurements use the shared kernel monotonic clock. Round trips are
measured directly, never divided by two. These are end-to-end Native API costs,
not isolated IRQ latency or pure scheduler dispatcher cycles. The clock case is
reported separately; its minimum is not subtracted from every sample.

## Run on a board

```sh
sched-bench cyclic --cpus 0-3 --duration-ms 10000 --interval-us 1000 \
  --repeat 3 --output /tmp/cyclic.json
sched-bench suite --cpus 0-3 --duration-ms 1000 --repeat 3 \
  --raw --max-raw 10000 --output /tmp/scheduler.json
sched-bench cyclic --cpus 1 --duration-ms 10000 --load-threads 2 \
  --output /tmp/cyclic-cpu-load.json
sched-bench cyclic --cpus 1 --duration-ms 10000 --load-threads 1 \
  --load memory --load-mib 8 --output /tmp/cyclic-memory-load.json
sched-bench channel --cpus 0,2 --cross-cpu --duration-ms 3000 \
  --output /tmp/cross-cpu.json
```

Explicit CPU lists/ranges create one independently supervised worker with a
singleton affinity per selected CPU. Threads inside that worker inherit the
mask. Cross-CPU channels create a producer/receiver pair for each CPU, with
receivers on the next CPU in the selected list. Same-CPU channels pin both ends
to the same CPU. Offline CPUs are errors. The default `--cpus inherit` retains
the parent placement and is never described as a pinned or cross-CPU run.

Workers prepare, warm up and touch their measurement buffers before READY.
The coordinator then publishes a common future start and end. No console output
or histogram reduction occurs in the timed loops. Allocation is outside each
timed sample, except the intentional thread-creation workload. Reports are
collected only after all workers finish. Background load and kernel/system/VM
activity can still influence the results; pinning does not reserve a CPU.

`--duration-ms` bounds the measured phase, not preparation/export. `--iterations`
also caps samples in latency cases; fairness always uses the duration.
`--warmup` defaults to 20 operations, omitted from statistics. `--threads`
controls yield/fairness competitors; atomic always uses two threads, cyclic one
per selected CPU. `--load-threads` adds same-worker CPU or memory contention.
All runs have bounded preparation/protocol deadlines via `--timeout-ms`.
Failures stop and reap only this invocation's children, not the caller's task
group. A failed run returns nonzero and is not a successful performance result.

## Periods and distributions

The default `--overrun skip` keeps the original phase and advances past expired
releases. `skipped_periods` counts missed releases, and `overruns` counts observed
wakeups at least one interval late. `--overrun catch-up` keeps every release and
allows immediate catch-up operations; those are not independent timer wakeups.
A wake past the run end stops catch-up and counts the remaining in-window
releases as skipped; it does not continue draining an unbounded backlog.
A requested 1 ms period does not imply a 1 ms effective kernel wakeup granularity.
Read missed-period and overflow counters alongside latency.

JSON includes exact count/min/integer mean/max, a histogram, percentile bounds,
actual scheduling parameters, placement, work counts, and run options. Default
bins are 1,000 ns wide with 32,768 bins. `p50/p90/p99/p999` are nearest-rank
**inclusive histogram upper bounds**, not exact quantiles. If a quantile falls
in overflow it is null; the real maximum and overflow count remain available.
Increase `--histogram-bin-ns` or `--histogram-bins` for long tails.

`--raw` retains the first `--max-raw` timestamp pairs per measured series;
`raw_dropped` explicitly counts later samples. This is bounded prefix sampling,
not reservoir sampling. Empty series have null latency statistics, not a false
zero. Histogram/raw storage and retained repetitions are bounded before launch.
`--output` creates a new JSON file and refuses to overwrite existing evidence.
Machine files use compact JSON to avoid repeating indentation for every bin.
`--json` additionally prints JSON; prefer file output for large reports.

## Authority and measurement limits

By default the tool follows Native child scheduling inheritance rules. Explicit `--policy fair|fifo|budget`,
`--band`, and policy-specific weight/quantum/priority/budget parameters are
checked through existing Native scheduling capabilities. Self-lowering may be
allowed; raising or changing class requires already-delegated rights. The tool
does not acquire admin authority or fall back to another policy when denied.
Each measured thread reports its observed parameters. Background workers use
ordinary inherited thread-creation policy; they are not automatically promoted
to FIFO. Changing a policy changes what is being measured and may intentionally
starve equal/lower-priority competitors.

Buffers are touched before timing but are not guaranteed resident by default.
`--lock-memory` requires the existing `LOCK_MEMORY` factory authority, populates
and locks current worker mappings before READY, and unlocks before export.
Future mappings, including newly created thread stacks in `spawn`, are excluded.
Permission failures are explicit; normal shell authority is not expanded.

Use consistent image/configuration, CPU placement, policy, intervals and load
for revision comparisons. Record the image identity, repeat long board runs,
and inspect distributions rather than comparing only averages. A short maximum
does not prove a worst-case real-time bound. CI checks functionality on one and
four QEMU CPUs; TCG timings are not hardware performance claims.

See the [host matrix runner and CSV exporter](https://github.com/HypeR-Proj/ci/blob/main/performance/scheduler/README.md)
for reproducible artifacts and comparison guidance. For a critical-chain trace,
run the benchmark under [`perf trace record`](https://hyper-proj.github.io/perf/docs/tracing.html); enable tracing only
in a separate diagnostic run because tracing itself perturbs latency.
