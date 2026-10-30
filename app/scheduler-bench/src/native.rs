// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Native-only measurement primitives. Process creation enforces CPU placement;
//! no scheduler implementation or raw syscall ABI is imported by the benchmark.
mod control;
mod measure;
mod supervisor;

use crate::config::Options;
use crate::stats::{Recorder, Series};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
};
pub use supervisor::{run, worker};

type Result<T> = std::result::Result<T, String>;
fn now() -> Result<u64> {
    hyper_os::time::monotonic_now()
        .map(|n| n.as_nanoseconds())
        .map_err(|e| format!("monotonic clock: {e:?}"))
}
fn after(ms: u64) -> Result<u64> {
    now()?
        .checked_add(ms.checked_mul(1_000_000).ok_or("deadline overflow")?)
        .filter(|n| *n != u64::MAX)
        .ok_or("deadline overflow".into())
}
fn sleep_until(target: u64) -> Result<u64> {
    loop {
        hyper_os::thread::sleep_until(target).map_err(|e| format!("absolute sleep: {e:?}"))?;
        let time = now()?;
        if time >= target {
            return Ok(time);
        }
        // A wake before the target is not a negative/zero-latency timer sample.
    }
}
fn scheduling() -> String {
    hyper_os::scheduling::current()
        .map(|p| format!("{p:?}"))
        .unwrap_or_else(|e| format!("unavailable: {e:?}"))
}
fn recorder(o: &Options) -> Result<Recorder> {
    Recorder::new(
        o.histogram_bin_ns,
        o.histogram_bins,
        o.raw.then_some(o.max_raw),
    )
    .map_err(|e| e.to_string())
}
struct PendingSeries {
    name: &'static str,
    worker: usize,
    scheduling: String,
    recorder: Recorder,
    start: u64,
    end: u64,
    work: u64,
    skipped_periods: u64,
    overruns: u64,
}
impl PendingSeries {
    fn finish(self) -> Series {
        Series {
            name: self.name.into(),
            worker: self.worker,
            clock_domain: "kernel monotonic nanoseconds".into(),
            scheduling: self.scheduling,
            distribution: self.recorder.finish(),
            skipped_periods: self.skipped_periods,
            overruns: self.overruns,
            elapsed_ns: self.end.saturating_sub(self.start),
            work_units: self.work,
        }
    }
}
fn series(
    name: &'static str,
    worker: usize,
    scheduling: String,
    r: Recorder,
    start: u64,
    end: u64,
    work: u64,
) -> PendingSeries {
    PendingSeries {
        name,
        worker,
        scheduling,
        recorder: r,
        start,
        end,
        work,
        skipped_periods: 0,
        overruns: 0,
    }
}
fn keep_running(o: &Options, count: u64, end: u64) -> Result<bool> {
    Ok((o.iterations == 0 || count < o.iterations) && now()? < end)
}

/// Release/acquire gates are process-private futex words, not timing samples.
struct Gate {
    phase: AtomicU32,
    start: AtomicU64,
    end: AtomicU64,
    cancel: AtomicBool,
}
impl Gate {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            phase: AtomicU32::new(0),
            start: AtomicU64::new(0),
            end: AtomicU64::new(0),
            cancel: AtomicBool::new(false),
        })
    }
    fn start(&self, start: u64, end: u64) -> Result<()> {
        self.start.store(start, Ordering::Relaxed);
        self.end.store(end, Ordering::Relaxed);
        self.phase.store(1, Ordering::Release);
        hyper_os::thread::atomic_wake(&self.phase, u32::MAX)
            .map_err(|e| format!("start wake: {e:?}"))?;
        Ok(())
    }
    fn wait(&self, deadline: u64) -> Result<(u64, u64)> {
        while self.phase.load(Ordering::Acquire) == 0 {
            if now()? >= deadline {
                return Err("worker preparation/start timed out".into());
            }
            hyper_os::thread::atomic_wait(&self.phase, 0, deadline)
                .map_err(|e| format!("start wait: {e:?}"))?;
        }
        if self.cancel.load(Ordering::Acquire) {
            return Err("benchmark cancelled".into());
        }
        let start = self.start.load(Ordering::Relaxed);
        let end = self.end.load(Ordering::Relaxed);
        sleep_until(start)?;
        Ok((start, end))
    }
    fn abort(&self) {
        self.cancel.store(true, Ordering::Release);
        self.phase.store(1, Ordering::Release);
        let _ = hyper_os::thread::atomic_wake(&self.phase, u32::MAX);
    }
}

/// Identical integer work chunks, with a compiler barrier to retain the work.
fn burn(state: &mut u64) {
    for _ in 0..64 {
        *state = std::hint::black_box(
            state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407),
        );
    }
}
