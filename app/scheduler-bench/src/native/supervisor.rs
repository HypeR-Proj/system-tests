// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

use super::{
    control::{self, CHUNK, CONTROL, Channel, DATA, DONE, END, FAILURE, MAX_REPORT, PEER, READY},
    measure::Prepared,
    *,
};
use crate::config::{Case, Command, Job, Options, Policy, cpu_list};
use crate::stats::{WorkerReport, jain};
use hyper_os::{
    Rights,
    fs::Directory,
    handle::{ByteChannelObject, OwnedHandle, ProcessObject, RightsOffer},
    scheduling::{FairBackend, Parameters, Policy as NativePolicy},
    startup,
    task::{ProcessBuilder, ProcessTermination},
};
use hyper_service::process::{self, LoaderKind};
use serde::Serialize;

fn parameters(o: &Options, current: Parameters) -> Parameters {
    Parameters {
        band: o.band.unwrap_or(current.band),
        role: current.role,
        policy: match o.policy {
            Policy::Inherit => current.policy,
            Policy::Fair => NativePolicy::Fair {
                weight: o.weight,
                quantum_ns: o.quantum_us * 1000,
                backend: FairBackend::Default,
            },
            Policy::Fifo => NativePolicy::Fifo {
                priority: o.fifo_priority,
            },
            Policy::Budget => NativePolicy::Budget {
                budget_ns: o.budget_us * 1000,
                period_ns: o.period_us * 1000,
                burst_ns: o.budget_us * 1000,
            },
        },
    }
}
fn policy_requested(o: &Options) -> bool {
    o.policy != Policy::Inherit || o.band.is_some()
}
pub(super) fn apply_policy(o: &Options) -> Result<()> {
    if policy_requested(o) {
        let current =
            hyper_os::scheduling::current().map_err(|e| format!("read scheduling: {e:?}"))?;
        let desired = parameters(o, current);
        // Default resolves to the installed concrete backend. Applying it is
        // checked by the kernel; failure must never produce mislabeled results.
        if desired != current {
            hyper_os::scheduling::set_current(desired).map_err(|e| {
                format!("scheduling change denied/unsupported (no fallback): {e:?}")
            })?;
        }
    }
    Ok(())
}

struct Child {
    process: OwnedHandle<ProcessObject>,
    control: Channel,
    timeout_ms: u64,
    complete: bool,
}
impl Drop for Child {
    fn drop(&mut self) {
        if !self.complete {
            let supervisor = self.process.as_process_supervisor();
            let _ = supervisor.request_stop();
            if let Ok(deadline) = after(self.timeout_ms) {
                let _ = supervisor.wait_terminated(deadline);
            }
        }
    }
}
fn spawn(
    startup: &startup::Startup<'_>,
    root: &Directory,
    job: &Job,
    peer: Option<OwnedHandle<ByteChannelObject>>,
) -> Result<Child> {
    let image = root
        .open(&job.options.worker_image, process::EXECUTABLE_FILE_RIGHTS)
        .map_err(|e| format!("open worker {}: {e:?}", job.options.worker_image))?;
    let loader = process::open_loader(root, LoaderKind::Dynamic)
        .map_err(|e| format!("open loader: {e:?}"))?;
    let factory = startup
        .borrow(startup::TASK_FACTORY)
        .map_err(|e| format!("task factory: {e:?}"))?;
    let builder = ProcessBuilder::create(
        factory,
        startup
            .borrow(startup::TASK_GROUP)
            .map_err(|e| format!("task group: {e:?}"))?,
        startup
            .borrow(startup::RESOURCE_DOMAIN)
            .map_err(|e| format!("resource domain: {e:?}"))?,
        image.as_handle_ref(),
        loader.as_handle_ref(),
    )
    .map_err(|e| format!("create worker: {e:?}"))?;
    builder
        .set_name(&format!(
            "sched-{:?}-{}",
            job.case,
            job.cpu.map_or("inherit".into(), |n| n.to_string())
        ))
        .map_err(|e| format!("name: {e:?}"))?;
    builder
        .add_argument(&job.options.worker_image)
        .map_err(|e| format!("argv: {e:?}"))?;
    builder
        .add_argument("worker")
        .map_err(|e| format!("argv: {e:?}"))?;
    builder
        .add_argument(&serde_json::to_string(job).map_err(|e| e.to_string())?)
        .map_err(|e| format!("config: {e:?}"))?;
    if let Some(cpu) = job.cpu {
        let mut words = vec![0u64; usize::from(cpu) / 64 + 1];
        words[usize::from(cpu) / 64] |= 1u64 << (cpu % 64);
        builder
            .set_affinity(&words)
            .map_err(|e| format!("CPU {cpu} affinity rejected (no fallback): {e:?}"))?;
    }
    if policy_requested(&job.options) {
        let current =
            hyper_os::scheduling::current().map_err(|e| format!("read scheduling: {e:?}"))?;
        let available = factory
            .info()
            .map_err(|e| format!("factory rights: {e:?}"))?
            .rights;
        let rights = Rights::from_bits(available.bits() & hyper_os::scheduling::RIGHTS.bits())
            .ok_or("invalid factory scheduling rights")?;
        if rights != Rights::NONE {
            builder
                .set_scheduling(parameters(&job.options, current), rights)
                .map_err(|e| {
                    format!("requested scheduling requires delegated capability: {e:?}")
                })?;
        }
    }
    if job.options.lock_memory {
        builder
            .set_memory_lock_allowed(true)
            .map_err(|e| format!("memory lock requires LOCK_MEMORY capability: {e:?}"))?;
    }
    let libs = process::open_library_directory(root).map_err(|e| format!("libraries: {e:?}"))?;
    builder
        .add_handle_duplicate(
            libs.as_handle_ref(),
            startup::DYNAMIC_LIBRARY_DIRECTORY.as_raw(),
            RightsOffer::Exact(Rights::READ.union(Rights::EXECUTE).union(Rights::PROTECT)),
        )
        .map_err(|e| format!("delegate libraries: {e:?}"))?;
    let (control, child) = control::pair()?;
    let rights = Rights::READ.union(Rights::WRITE).union(Rights::WAIT);
    builder
        .add_handle_move(child, CONTROL.as_raw(), RightsOffer::Exact(rights))
        .map_err(|e| format!("delegate control: {:?}", e.error()))?;
    if let Some(peer) = peer {
        builder
            .add_handle_move(peer, PEER.as_raw(), RightsOffer::Exact(rights))
            .map_err(|e| format!("delegate peer: {:?}", e.error()))?;
    }
    builder.seal().map_err(|e| format!("seal worker: {e:?}"))?;
    let process = builder
        .start()
        .map_err(|e| format!("start worker: {:?}", e.error()))?;
    Ok(Child {
        process,
        control,
        timeout_ms: job.options.timeout_ms,
        complete: false,
    })
}

#[derive(Serialize)]
struct Report {
    format: &'static str,
    architecture: &'static str,
    options: Options,
    metrics_note: &'static str,
    results: Vec<WorkerReport>,
    skipped: Vec<String>,
}

pub fn run(command: Command) -> Result<()> {
    let (case, suite, o) = match command {
        Command::Suite(o) => (Case::Clock, true, o),
        Command::Clock(o) => (Case::Clock, false, o),
        Command::Cyclic(o) => (Case::Cyclic, false, o),
        Command::Yield(o) => (Case::Yield, false, o),
        Command::Atomic(o) => (Case::Atomic, false, o),
        Command::Channel(o) => (Case::Channel, false, o),
        Command::Fairness(o) => (Case::Fairness, false, o),
        Command::Spawn(o) => (Case::Spawn, false, o),
        Command::Worker { .. } => return Err("worker is an internal startup route".into()),
    };
    o.validate()?;
    if o.cross_cpu && case != Case::Channel && !suite {
        return Err("--cross-cpu is only meaningful for channel or suite".into());
    }
    let cpus = cpu_list(&o.cpus)?;
    let mut startup = hyper_rt::process::startup().map_err(|e| format!("startup: {e:?}"))?;
    let root = Directory::from_handle(
        startup
            .take(startup::ROOT_DIRECTORY)
            .map_err(|e| format!("root: {e:?}"))?,
    );
    let mut report = Report {
        format: "hyper.scheduler-bench.v1",
        architecture: std::env::consts::ARCH,
        options: o.clone(),
        metrics_note: "Elapsed Native API latency includes timer/IPC/syscall/runtime costs; not pure scheduler dispatch or hardware interrupt latency. Histogram percentiles are inclusive upper bounds. Fairness measures identical-work progress, not CPU utilization.",
        results: Vec::new(),
        skipped: Vec::new(),
    };
    let mut cases = if suite {
        vec![
            (Case::Clock, false),
            (Case::Cyclic, false),
            (Case::Yield, false),
            (Case::Atomic, false),
            (Case::Channel, false),
            (Case::Fairness, false),
            (Case::Spawn, false),
        ]
    } else {
        vec![(case, o.cross_cpu)]
    };
    if suite {
        if cpus.len() > 1 && cpus.iter().all(Option::is_some) {
            cases.push((Case::Channel, true));
        } else {
            report.skipped.push("channel cross-CPU: requires two or more explicit singleton CPUs; inherited placement is not called cross-CPU".into());
        }
    }
    let series_per_cpu = cases
        .iter()
        .map(|(case, _)| match case {
            Case::Yield | Case::Fairness => o.threads,
            Case::Atomic | Case::Channel => 2,
            _ => 1,
        })
        .sum::<usize>();
    o.validate_retained(series_per_cpu * cpus.len())?;
    if let Some(path) = &o.output
        && std::path::Path::new(path)
            .try_exists()
            .map_err(|e| format!("output path: {e}"))?
    {
        return Err(format!("output already exists: {path}"));
    }
    for repetition in 0..o.repeat {
        for &(case, cross) in &cases {
            if cross && (cpus.len() < 2 || cpus.iter().any(Option::is_none)) {
                return Err("cross-CPU channel requires at least two explicit CPUs".into());
            }
            let mut children = Vec::new();
            for (index, &cpu) in cpus.iter().enumerate() {
                let mut options = o.clone();
                options.cross_cpu = cross;
                let mut job = Job {
                    case,
                    options,
                    cpu,
                    receiver: false,
                    peer_cpu: None,
                    repetition,
                };
                if case == Case::Channel {
                    let peer_cpu = if cross {
                        cpus[(index + 1) % cpus.len()]
                    } else {
                        cpu
                    };
                    job.peer_cpu = peer_cpu;
                    let (local, remote) = control::pair()?;
                    children.push(spawn(&startup, &root, &job, Some(local.0))?);
                    job.receiver = true;
                    job.cpu = peer_cpu;
                    job.peer_cpu = cpu;
                    children.push(spawn(&startup, &root, &job, Some(remote))?);
                } else {
                    children.push(spawn(&startup, &root, &job, None)?);
                }
            }
            for child in &children {
                child.control.expect(READY, after(o.timeout_ms)?)?;
            }
            let start = after(100)?;
            let end = start
                .checked_add(o.duration_ms * 1_000_000)
                .ok_or("run end overflow")?;
            let deadline = end
                .checked_add(o.timeout_ms * 1_000_000)
                .ok_or("run deadline overflow")?;
            let mut frame = [0u8; 17];
            frame[0] = b'G';
            frame[1..9].copy_from_slice(&start.to_le_bytes());
            frame[9..].copy_from_slice(&end.to_le_bytes());
            for child in &children {
                child.control.send(&frame, deadline)?;
            }
            for child in &children {
                child.control.expect(DONE, deadline)?;
            }
            // Bulk serialization and collection begin only after all measurements stop.
            for child in &mut children {
                let deadline = after(o.timeout_ms)?;
                child.control.marker(b'D', deadline)?;
                let mut bytes = Vec::new();
                let mut frame = [0u8; CHUNK];
                loop {
                    let size = child.control.receive(&mut frame, deadline)?;
                    if size == 1 && frame[0] == END {
                        break;
                    }
                    if size < 2 || frame[0] != DATA {
                        return Err("malformed report frame".into());
                    }
                    if bytes.len() + size - 1 > MAX_REPORT {
                        return Err("worker report exceeds 256 MiB".into());
                    }
                    bytes.extend_from_slice(&frame[1..size]);
                }
                let result: WorkerReport =
                    serde_json::from_slice(&bytes).map_err(|e| format!("worker report: {e}"))?;
                child
                    .process
                    .as_process_supervisor()
                    .wait_terminated(deadline)
                    .map_err(|e| format!("worker exit timeout: {e:?}"))?;
                let terminal = child
                    .process
                    .as_process_supervisor()
                    .info()
                    .map_err(|e| format!("worker status: {e:?}"))?
                    .terminal;
                if !matches!(
                    terminal,
                    Some(ProcessTermination::ProcessExited { status: 0 })
                        | Some(ProcessTermination::LastThreadExited { status: 0 })
                        | Some(ProcessTermination::ThreadExited { status: 0 })
                ) {
                    return Err(format!("worker abnormal termination: {terminal:?}"));
                }
                child.complete = true;
                report.results.push(result);
            }
        }
    }
    if let Some(path) = &o.output {
        use std::io::Write;
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)
            .map_err(|e| format!("create output {path} (existing files are preserved): {e}"))?;
        // Machine artifacts retain all samples without per-bin indentation.
        // Thousands of bins across CPUs/repetitions otherwise waste disk/RAM.
        let mut output = std::io::BufWriter::with_capacity(64 * 1024, file);
        serde_json::to_writer(&mut output, &report).map_err(|e| format!("encode report: {e}"))?;
        output
            .write_all(b"\n")
            .and_then(|_| output.flush())
            .and_then(|_| output.get_ref().sync_all())
            .map_err(|e| format!("write report: {e}"))?;
    }
    if o.json {
        use std::io::Write;
        let mut stdout = std::io::stdout().lock();
        serde_json::to_writer_pretty(&mut stdout, &report)
            .map_err(|e| format!("encode report: {e}"))?;
        stdout.write_all(b"\n").map_err(|e| e.to_string())?;
    } else {
        println!(
            "CASE      CPU      RUN THR SERIES                        SAMPLES   MEAN us  P99 upper us      MAX us"
        );
        for result in &report.results {
            for series in &result.series {
                let d = &series.distribution;
                let us =
                    |n: Option<u64>| n.map_or("-".into(), |n| format!("{:.3}", n as f64 / 1000.0));
                println!(
                    "{:<9} {:<8} {:>3} {:>3} {:<29} {:>8} {:>9} {:>13} {:>11}",
                    format!("{:?}", result.job.case),
                    result.job.cpu.map_or("inherit".into(), |c| c.to_string()),
                    result.job.repetition + 1,
                    series.worker,
                    series.name,
                    d.count,
                    us(d.mean_ns),
                    us(d.p99_upper_ns),
                    us(d.max_ns)
                );
                if d.overflow != 0
                    || series.skipped_periods != 0
                    || series.overruns != 0
                    || d.raw_dropped != 0
                {
                    println!(
                        "  overflow={} skipped-periods={} overruns={} raw-dropped={}",
                        d.overflow, series.skipped_periods, series.overruns, d.raw_dropped
                    );
                }
            }
            if let Some(fairness) = result.fairness {
                println!("  progress Jain index={fairness:.6} (not CPU usage)");
            }
        }
        for reason in &report.skipped {
            println!("SKIP {reason}");
        }
        if let Some(path) = &o.output {
            println!("Report: {path}");
        }
    }
    Ok(())
}

pub fn worker(text: &str) -> Result<()> {
    let job: Job = serde_json::from_str(text).map_err(|e| format!("worker config: {e}"))?;
    job.options.validate()?;
    let mut startup = hyper_rt::process::startup().map_err(|e| format!("startup: {e:?}"))?;
    let control = Channel(
        startup
            .take(CONTROL)
            .map_err(|e| format!("worker requires supervisor control: {e:?}"))?,
    );
    let result = (|| -> Result<()> {
        apply_policy(&job.options)?;
        let mut prepared = Prepared::new(&job)?;
        let peer = if job.case == Case::Channel {
            Some(Channel(
                startup.take(PEER).map_err(|e| format!("peer: {e:?}"))?,
            ))
        } else {
            None
        };
        if let Some(peer) = &peer {
            channel_warmup(&job, peer)?;
        }
        let samples = if job.case == Case::Channel {
            Some(recorder(&job.options)?)
        } else {
            None
        };
        if job.options.lock_memory {
            hyper_os::memory::lock_current_memory(
                startup
                    .borrow(startup::ROOT_VMAR)
                    .map_err(|e| format!("VMAR: {e:?}"))?,
            )
            .map_err(|e| format!("lock current memory: {e:?}"))?;
        }
        control.marker(READY, after(job.options.timeout_ms)?)?;
        let mut frame = [0u8; CHUNK];
        let size = control.receive(&mut frame, after(job.options.timeout_ms)?)?;
        if size != 17 || frame[0] != b'G' {
            return Err("invalid start command".into());
        }
        let word = |range: std::ops::Range<usize>| -> Result<u64> {
            Ok(u64::from_le_bytes(
                frame[range].try_into().map_err(|_| "bad start word")?,
            ))
        };
        let start = word(1..9)?;
        let end = word(9..17)?;
        if start >= end || end - start != job.options.duration_ms * 1_000_000 {
            return Err("invalid measurement window".into());
        }
        prepared.start(start, end)?;
        let mut pending = Vec::new();
        if let Some(peer) = peer {
            sleep_until(start)?;
            pending.push(channel_case(
                &job,
                &peer,
                samples.ok_or("missing channel sample buffer")?,
                start,
                end,
            )?);
        }
        let (mut measured, load) = prepared.finish()?;
        pending.append(&mut measured);
        control.marker(DONE, after(job.options.timeout_ms)?)?;
        // Reports are pulled serially only after *all* workers finish. Give the
        // last member its bounded collection window, rather than timing out
        // merely because earlier members have large histograms to export.
        let workers = cpu_list(&job.options.cpus)?.len() as u64
            * if job.case == Case::Channel { 2 } else { 1 };
        control.expect(b'D', after(job.options.timeout_ms * workers)?)?;
        if job.options.lock_memory {
            hyper_os::memory::unlock_current_memory(
                startup
                    .borrow(startup::ROOT_VMAR)
                    .map_err(|e| format!("VMAR: {e:?}"))?,
            )
            .map_err(|e| format!("unlock memory: {e:?}"))?;
        }
        let series: Vec<_> = pending.into_iter().map(PendingSeries::finish).collect();
        let fairness = if job.case == Case::Fairness {
            jain(&series.iter().map(|s| s.work_units).collect::<Vec<_>>())
        } else {
            None
        };
        let report = WorkerReport {
            job: job.clone(),
            start_ns: start,
            end_ns: end,
            memory_mode: if job.options.lock_memory {
                "current mappings locked; future mappings excluded"
            } else {
                "prefaulted measurement buffers; not residency locked"
            }
            .into(),
            series,
            fairness,
            load_work_units: load,
        };
        let bytes = serde_json::to_vec(&report).map_err(|e| e.to_string())?;
        if bytes.len() > MAX_REPORT {
            return Err("report too large".into());
        }
        let deadline = after(job.options.timeout_ms)?;
        for part in bytes.chunks(CHUNK - 1) {
            let mut frame = [0; CHUNK];
            frame[0] = DATA;
            frame[1..part.len() + 1].copy_from_slice(part);
            control.send(&frame[..part.len() + 1], deadline)?;
        }
        control.marker(END, deadline)?;
        Ok(())
    })();
    if let Err(error) = &result
        && let Ok(deadline) = after(1000)
    {
        let mut buf = vec![FAILURE];
        buf.extend_from_slice(&error.as_bytes()[..error.len().min(CHUNK - 1)]);
        let _ = control.send(&buf, deadline);
    }
    result
}

fn channel_case(
    job: &Job,
    peer: &Channel,
    mut samples: Recorder,
    start: u64,
    end: u64,
) -> Result<PendingSeries> {
    let policy = scheduling();
    let timeout = job.options.timeout_ms * 1_000_000;
    let deadline = end
        .checked_add(timeout)
        .ok_or("channel deadline overflow")?;
    let mut count = 0u64;
    let mut frame = [0u8; 17];
    if job.receiver {
        loop {
            let size = peer.receive(&mut frame, deadline)?;
            if size == 1 && frame[0] == b'X' {
                break;
            }
            let observed = now()?;
            if size != 17 || frame[0] != b'P' {
                return Err("bad ping frame".into());
            }
            let sequence = u64::from_le_bytes(frame[1..9].try_into().map_err(|_| "bad sequence")?);
            let stamp = u64::from_le_bytes(frame[9..17].try_into().map_err(|_| "bad timestamp")?);
            if sequence != count {
                return Err("channel sequence mismatch".into());
            }
            samples.record(stamp, observed)?;
            count += 1;
            peer.send(&frame, deadline)?;
        }
        Ok(series(
            "channel one-way handoff",
            1,
            policy,
            samples,
            start,
            now()?,
            count,
        ))
    } else {
        let outcome = (|| -> Result<()> {
            while keep_running(&job.options, count, end)? {
                frame[0] = b'P';
                frame[1..9].copy_from_slice(&count.to_le_bytes());
                let before = now()?;
                frame[9..17].copy_from_slice(&before.to_le_bytes());
                peer.send(&frame, deadline)?;
                let mut reply = [0u8; 17];
                let size = peer.receive(&mut reply, deadline)?;
                let observed = now()?;
                if size != 17 || reply != frame {
                    return Err("invalid ping reply".into());
                }
                samples.record(before, observed)?;
                count += 1;
            }
            Ok(())
        })();
        let stop = peer.send(b"X", deadline);
        outcome?;
        stop?;
        Ok(series(
            "channel round trip",
            0,
            policy,
            samples,
            start,
            now()?,
            count,
        ))
    }
}

fn channel_warmup(job: &Job, peer: &Channel) -> Result<()> {
    let deadline = after(job.options.timeout_ms)?;
    let mut frame = [0u8; 17];
    frame[0] = b'W';
    for sequence in 0..job.options.warmup {
        if job.receiver {
            let size = peer.receive(&mut frame, deadline)?;
            if size != 17
                || frame[0] != b'W'
                || u64::from_le_bytes(frame[1..9].try_into().map_err(|_| "warmup sequence")?)
                    != sequence
            {
                return Err("invalid warmup ping".into());
            }
            std::hint::black_box(now()?);
            peer.send(&frame, deadline)?;
        } else {
            frame[1..9].copy_from_slice(&sequence.to_le_bytes());
            frame[9..17].copy_from_slice(&now()?.to_le_bytes());
            peer.send(&frame, deadline)?;
            let mut reply = [0u8; 17];
            let size = peer.receive(&mut reply, deadline)?;
            if size != 17 || reply != frame {
                return Err("invalid warmup reply".into());
            }
        }
    }
    Ok(())
}
