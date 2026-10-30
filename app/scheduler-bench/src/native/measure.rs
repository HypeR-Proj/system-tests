// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

use super::*;
use crate::config::{Case, Job, Load, next_release};
use std::{
    sync::mpsc,
    thread::{self, JoinHandle},
};

pub(super) struct Prepared {
    gate: Arc<Gate>,
    exchange: Arc<Exchange>,
    stop: Arc<AtomicBool>,
    threads: Vec<JoinHandle<Result<Vec<PendingSeries>>>>,
    loads: Vec<JoinHandle<Result<u64>>>,
}
impl Drop for Prepared {
    fn drop(&mut self) {
        self.gate.abort();
        self.exchange.turn.store(2, Ordering::Release);
        let _ = hyper_os::thread::atomic_wake(&self.exchange.turn, u32::MAX);
        self.stop.store(true, Ordering::Release);
        // Every worker also has a finite absolute endpoint. No Rust thread is
        // asynchronously killed, and no shared buffers outlive their owners.
        for thread in self.threads.drain(..) {
            let _ = thread.join();
        }
        for thread in self.loads.drain(..) {
            let _ = thread.join();
        }
    }
}
impl Prepared {
    pub(super) fn new(job: &Job) -> Result<Self> {
        let mut result = Self {
            gate: Gate::new(),
            exchange: Arc::new(Exchange {
                turn: AtomicU32::new(0),
                stamp: AtomicU64::new(0),
            }),
            stop: Arc::new(AtomicBool::new(false)),
            threads: Vec::new(),
            loads: Vec::new(),
        };
        let (tx, rx) = mpsc::channel();
        let exchange = result.exchange.clone();
        let count = match job.case {
            Case::Yield | Case::Fairness => job.options.threads,
            Case::Atomic => 2,
            Case::Channel => 0,
            _ => 1,
        };
        for id in 0..count {
            let exchange = exchange.clone();
            let job = job.clone();
            let gate = result.gate.clone();
            let stop = result.stop.clone();
            let ready = tx.clone();
            result.threads.push(
                thread::Builder::new()
                    .name(format!("sched-{:?}-{id}", job.case))
                    .spawn(move || {
                        let preparation = (|| -> Result<(String, Recorder)> {
                            super::supervisor::apply_policy(&job.options)?;
                            let policy = scheduling();
                            let samples = recorder(&job.options)?;
                            // Exercise clocks and the measured primitive before signalling readiness.
                            if job.case == Case::Atomic {
                                atomic_warmup(&job.options, id, &exchange)?;
                            }
                            for _ in 0..if job.case == Case::Atomic {
                                0
                            } else {
                                job.options.warmup
                            } {
                                match job.case {
                                    Case::Cyclic => {
                                        let target = after(1)?;
                                        sleep_until(target)?;
                                    }
                                    Case::Yield => hyper_os::task::yield_now()
                                        .map_err(|e| format!("warmup yield: {e:?}"))?,
                                    Case::Spawn => {
                                        thread::Builder::new()
                                            .name("sched-warm-entry".into())
                                            .spawn(now)
                                            .map_err(|e| format!("warmup spawn: {e}"))?
                                            .join()
                                            .map_err(|_| "warmup entry worker panicked")??;
                                    }
                                    Case::Fairness => {
                                        let mut state = id as u64 + 1;
                                        burn(&mut state);
                                        std::hint::black_box(state);
                                    }
                                    _ => {
                                        std::hint::black_box(now()?);
                                    }
                                }
                            }
                            Ok((policy, samples))
                        })();
                        let (policy, mut samples) = match preparation {
                            Ok(value) => {
                                ready.send(Ok(())).map_err(|_| "preparation owner closed")?;
                                value
                            }
                            Err(error) => {
                                let _ = ready.send(Err(error.clone()));
                                return Err(error);
                            }
                        };
                        let (start, end) = gate.wait(after(job.options.timeout_ms)?)?;
                        match job.case {
                            Case::Cyclic => {
                                cyclic(&job.options, id, policy, samples, start, end, &stop)
                            }
                            Case::Fairness => {
                                let mut work = 0;
                                let mut state = id as u64 + 1;
                                while !stop.load(Ordering::Acquire) && now()? < end {
                                    burn(&mut state);
                                    work += 1;
                                }
                                let finish = now()?;
                                Ok(vec![series(
                                    "equal-work progress",
                                    id,
                                    policy,
                                    samples,
                                    start,
                                    finish,
                                    work,
                                )])
                            }
                            Case::Spawn => {
                                let mut work = 0;
                                while !stop.load(Ordering::Acquire)
                                    && keep_running(&job.options, work, end)?
                                {
                                    let before = now()?;
                                    let t = thread::Builder::new()
                                        .name("sched-entry".into())
                                        .spawn(now)
                                        .map_err(|e| format!("spawn: {e}"))?;
                                    let entered =
                                        t.join().map_err(|_| "entry worker panicked")??;
                                    samples.record(before, entered)?;
                                    work += 1;
                                }
                                Ok(vec![series(
                                    "thread creation to entry",
                                    id,
                                    policy,
                                    samples,
                                    start,
                                    now()?,
                                    work,
                                )])
                            }
                            Case::Clock | Case::Yield => {
                                let mut work = 0;
                                while !stop.load(Ordering::Acquire)
                                    && keep_running(&job.options, work, end)?
                                {
                                    let before = now()?;
                                    if job.case == Case::Yield {
                                        hyper_os::task::yield_now()
                                            .map_err(|e| format!("yield: {e:?}"))?;
                                    }
                                    let observed = now()?;
                                    samples.record(before, observed)?;
                                    work += 1;
                                }
                                Ok(vec![series(
                                    if job.case == Case::Clock {
                                        "clock observation pair"
                                    } else {
                                        "yield elapsed"
                                    },
                                    id,
                                    policy,
                                    samples,
                                    start,
                                    now()?,
                                    work,
                                )])
                            }
                            Case::Atomic => atomic(
                                &job.options,
                                id,
                                policy,
                                samples,
                                (start, end),
                                &stop,
                                &exchange,
                            ),
                            Case::Channel => Err("channel uses its process peer".into()),
                        }
                    })
                    .map_err(|e| format!("prepare worker: {e}"))?,
            );
        }
        for id in 0..job.options.load_threads {
            let gate = result.gate.clone();
            let stop = result.stop.clone();
            let ready = tx.clone();
            let o = job.options.clone();
            result.loads.push(
                thread::Builder::new()
                    .name(format!("sched-load-{id}"))
                    .spawn(move || {
                        let mut memory = if o.load == Load::Memory {
                            vec![1u64; o.load_mib * 1024 * 1024 / 8]
                        } else {
                            Vec::new()
                        };
                        for item in memory.iter_mut().step_by(512) {
                            *item = std::hint::black_box(7);
                        }
                        ready
                            .send(Ok(()))
                            .map_err(|_| "load preparation owner closed")?;
                        let (_, end) = gate.wait(after(o.timeout_ms)?)?;
                        let mut work = 0;
                        let mut state = id as u64 + 1;
                        while !stop.load(Ordering::Acquire) && now()? < end {
                            if memory.is_empty() {
                                burn(&mut state);
                            } else {
                                let index = (work as usize * 64) % memory.len();
                                memory[index] = std::hint::black_box(memory[index].wrapping_add(1));
                            }
                            work += 1;
                        }
                        Ok(work)
                    })
                    .map_err(|e| format!("prepare load: {e}"))?,
            );
        }
        drop(tx);
        for _ in 0..count + job.options.load_threads {
            rx.recv_timeout(std::time::Duration::from_millis(job.options.timeout_ms))
                .map_err(|e| format!("worker preparation failed: {e}"))??;
        }
        Ok(result)
    }
    pub(super) fn start(&self, start: u64, end: u64) -> Result<()> {
        self.gate.start(start, end)
    }
    pub(super) fn finish(&mut self) -> Result<(Vec<PendingSeries>, Vec<u64>)> {
        let mut series = Vec::new();
        let mut error = None;
        for thread in self.threads.drain(..) {
            match thread
                .join()
                .map_err(|_| "measurement worker panicked".to_string())
                .and_then(|r| r)
            {
                Ok(mut result) => series.append(&mut result),
                Err(e) => {
                    self.stop.store(true, Ordering::Release);
                    error = Some(e);
                }
            }
        }
        self.stop.store(true, Ordering::Release);
        let mut loads = Vec::new();
        for load in self.loads.drain(..) {
            match load
                .join()
                .map_err(|_| "load worker panicked".to_string())
                .and_then(|r| r)
            {
                Ok(n) => loads.push(n),
                Err(e) => error = Some(e),
            }
        }
        if let Some(error) = error {
            Err(error)
        } else {
            Ok((series, loads))
        }
    }
}
fn cyclic(
    o: &Options,
    id: usize,
    policy: String,
    mut r: Recorder,
    start: u64,
    end: u64,
    stop: &AtomicBool,
) -> Result<Vec<PendingSeries>> {
    let interval = o.interval_us * 1000;
    let mut target = start.checked_add(interval).ok_or("period overflow")?;
    let mut count = 0;
    let mut skipped = 0;
    let mut overruns = 0;
    while target < end
        && !stop.load(Ordering::Acquire)
        && (o.iterations == 0 || count < o.iterations)
    {
        let observed = sleep_until(target)?;
        r.record(target, observed)?;
        count += 1;
        if observed - target >= interval {
            overruns += 1;
        }
        let (next, lost) = next_release(target, observed, interval, o.overrun)
            .ok_or("release accounting overflow")?;
        // A late wake can land beyond the run end. Only releases belonging to
        // this measurement window count as missed, not future benchmark time.
        let remaining = (end - 1 - target) / interval;
        skipped += lost.min(remaining);
        if observed >= end {
            if o.overrun == crate::config::Overrun::CatchUp {
                skipped += remaining;
            }
            break;
        }
        target = next;
    }
    let mut result = series(
        "absolute periodic wakeup lateness",
        id,
        policy,
        r,
        start,
        now()?,
        count,
    );
    result.skipped_periods = skipped;
    result.overruns = overruns;
    Ok(vec![result])
}

struct Exchange {
    turn: AtomicU32,
    stamp: AtomicU64,
}
struct EndExchange<'a>(&'a Exchange);
impl Drop for EndExchange<'_> {
    fn drop(&mut self) {
        self.0.turn.store(2, Ordering::Release);
        let _ = hyper_os::thread::atomic_wake(&self.0.turn, u32::MAX);
    }
}
/// Round trips are measured directly; a one-way sample is never RTT / 2.
fn atomic(
    o: &Options,
    id: usize,
    policy: String,
    mut r: Recorder,
    window: (u64, u64),
    stop: &AtomicBool,
    exchange: &Exchange,
) -> Result<Vec<PendingSeries>> {
    let (start, end) = window;
    let _cleanup = EndExchange(exchange);
    let deadline = end
        .checked_add(o.timeout_ms * 1_000_000)
        .ok_or("atomic deadline overflow")?;
    let mut count = 0;
    if id == 1 {
        loop {
            while exchange.turn.load(Ordering::Acquire) == 0 {
                hyper_os::thread::atomic_wait(&exchange.turn, 0, deadline)
                    .map_err(|e| format!("atomic receiver wait: {e:?}"))?;
            }
            if exchange.turn.load(Ordering::Acquire) == 2 {
                break;
            }
            let observed = now()?;
            let before = exchange.stamp.load(Ordering::Relaxed);
            r.record(before, observed)?;
            count += 1;
            exchange.turn.store(0, Ordering::Release);
            hyper_os::thread::atomic_wake(&exchange.turn, 1)
                .map_err(|e| format!("atomic reply wake: {e:?}"))?;
        }
        Ok(vec![series(
            "atomic one-way handoff",
            id,
            policy,
            r,
            start,
            now()?,
            count,
        )])
    } else {
        while !stop.load(Ordering::Acquire) && keep_running(o, count, end)? {
            let before = now()?;
            exchange.stamp.store(before, Ordering::Relaxed);
            exchange
                .turn
                .compare_exchange(0, 1, Ordering::Release, Ordering::Acquire)
                .map_err(|_| "atomic receiver failed")?;
            hyper_os::thread::atomic_wake(&exchange.turn, 1)
                .map_err(|e| format!("atomic wake: {e:?}"))?;
            while exchange.turn.load(Ordering::Acquire) == 1 {
                hyper_os::thread::atomic_wait(&exchange.turn, 1, deadline)
                    .map_err(|e| format!("atomic sender wait: {e:?}"))?;
            }
            if exchange.turn.load(Ordering::Acquire) != 0 {
                return Err("atomic peer stopped".into());
            }
            r.record(before, now()?)?;
            count += 1;
        }
        Ok(vec![series(
            "atomic round trip",
            id,
            policy,
            r,
            start,
            now()?,
            count,
        )])
    }
}

fn atomic_warmup(o: &Options, id: usize, exchange: &Exchange) -> Result<()> {
    let deadline = after(o.timeout_ms)?;
    let result = (|| -> Result<()> {
        for _ in 0..o.warmup {
            if id == 0 {
                exchange.stamp.store(now()?, Ordering::Relaxed);
                exchange
                    .turn
                    .compare_exchange(0, 1, Ordering::Release, Ordering::Acquire)
                    .map_err(|_| "warmup peer failed")?;
                hyper_os::thread::atomic_wake(&exchange.turn, 1)
                    .map_err(|e| format!("warmup wake: {e:?}"))?;
                while exchange.turn.load(Ordering::Acquire) == 1 {
                    hyper_os::thread::atomic_wait(&exchange.turn, 1, deadline)
                        .map_err(|e| format!("warmup wait: {e:?}"))?;
                }
                if exchange.turn.load(Ordering::Acquire) != 0 {
                    return Err("warmup peer stopped".into());
                }
            } else {
                while exchange.turn.load(Ordering::Acquire) == 0 {
                    hyper_os::thread::atomic_wait(&exchange.turn, 0, deadline)
                        .map_err(|e| format!("warmup wait: {e:?}"))?;
                }
                if exchange.turn.load(Ordering::Acquire) != 1 {
                    return Err("warmup peer stopped".into());
                }
                std::hint::black_box(now()?);
                exchange.turn.store(0, Ordering::Release);
                hyper_os::thread::atomic_wake(&exchange.turn, 1)
                    .map_err(|e| format!("warmup wake: {e:?}"))?;
            }
        }
        Ok(())
    })();
    if result.is_err() {
        exchange.turn.store(2, Ordering::Release);
        let _ = hyper_os::thread::atomic_wake(&exchange.turn, u32::MAX);
    }
    result
}
