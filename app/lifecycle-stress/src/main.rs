// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Exercise real syscall cancellation, transfer and handle-page reclamation.

mod trace;

use hyper_os::{
    Error, Status,
    capability_channel::{CapabilityChannel, CapabilityDisposition, CapabilityReceiveSlot},
    channel,
    fs::Directory,
    handle::{
        ByteChannelObject, CapabilityChannelObject, OwnedHandle, ProcessObject, Rights, RightsOffer,
    },
    startup::{self, StartupPurpose},
    task::{ProcessBuilder, ProcessTermination},
    wait::{ObjectSignals, WaitItem, wait_many},
};
use hyper_service::process::{self, LoaderKind};
use std::{
    mem::MaybeUninit,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
const CONTROL: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x80ff_0030);
const TRANSFER: StartupPurpose<CapabilityChannelObject> = StartupPurpose::new(0x80ff_0031);
const BYTES: Rights = Rights::READ.union(Rights::WRITE).union(Rights::WAIT);

fn deadline() -> hyper_os::Result<u64> {
    Ok(hyper_os::time::deadline_after(Duration::from_secs(30))?.as_raw())
}

fn receive(channel: &OwnedHandle<ByteChannelObject>, output: &mut [u8]) -> hyper_os::Result<usize> {
    let limit = deadline()?;
    loop {
        match channel.as_byte_channel().try_receive(output) {
            Err(Error::Status(Status::WOULD_BLOCK)) => {
                wait_many(
                    &[WaitItem::new(
                        channel.as_handle_ref(),
                        ObjectSignals::<ByteChannelObject>::READABLE
                            .union(ObjectSignals::<ByteChannelObject>::PEER_CLOSED),
                    )],
                    limit,
                )?;
            }
            result => return result,
        }
    }
}

/// Failure paths stop their own child; never stop the shell's shared task group.
struct Child(OwnedHandle<ProcessObject>);
impl Drop for Child {
    fn drop(&mut self) {
        let _ = self.0.as_process_supervisor().request_stop();
        if let Ok(limit) = deadline() {
            let _ = self.0.as_process_supervisor().wait_terminated(limit);
        }
    }
}

fn launch(
    startup: &startup::Startup<'_>,
    root: &Directory,
    seed: u64,
) -> Result<(Child, OwnedHandle<ByteChannelObject>, CapabilityChannel)> {
    let image = root
        .open("/bin/lifecycle-stress", process::EXECUTABLE_FILE_RIGHTS)
        .map_err(|e| format!("open child image: {e:?}"))?;
    let loader = process::open_loader(root, LoaderKind::Dynamic)
        .map_err(|e| format!("open child loader: {e:?}"))?;
    let builder = ProcessBuilder::create(
        startup.borrow(startup::TASK_FACTORY)?,
        startup.borrow(startup::TASK_GROUP)?,
        startup.borrow(startup::RESOURCE_DOMAIN)?,
        image.as_handle_ref(),
        loader.as_handle_ref(),
    )
    .map_err(|e| format!("create child builder: {e:?}"))?;
    builder.set_name("stress-child")?;
    for arg in ["/bin/lifecycle-stress", "child", &seed.to_string()] {
        builder.add_argument(arg)?;
    }
    let libraries = process::open_library_directory(root)?;
    builder.add_handle_duplicate(
        libraries.as_handle_ref(),
        startup::DYNAMIC_LIBRARY_DIRECTORY.as_raw(),
        RightsOffer::Exact(Rights::READ.union(Rights::EXECUTE).union(Rights::PROTECT)),
    )?;
    let (control, child_control) = channel::create_pair()?;
    builder
        .add_handle_move(child_control, CONTROL.as_raw(), RightsOffer::Exact(BYTES))
        .map_err(|failure| failure.error())?;
    let (transfer, child_transfer) = CapabilityChannel::create()?;
    builder
        .add_handle_move(
            child_transfer.into_handle(),
            TRANSFER.as_raw(),
            RightsOffer::Exact(BYTES),
        )
        .map_err(|failure| failure.error())?;
    builder.seal().map_err(|e| format!("seal child: {e:?}"))?;
    let child = Child(
        builder
            .start()
            .map_err(|failure| format!("start child: {:?}", failure.error()))?,
    );
    Ok((child, control, transfer))
}

fn send_until_closed(transfer: CapabilityChannel, stopping: &AtomicBool) -> Result<usize> {
    let limit = Instant::now() + Duration::from_secs(60);
    for sequence in 0u64.. {
        if Instant::now() >= limit {
            return Err("sender did not observe peer closure".into());
        }
        let (peer, owner) = channel::create_pair()?;
        let mut source = Some(owner);
        loop {
            let sent = transfer.try_send(
                &sequence.to_le_bytes(),
                &mut [CapabilityDisposition::move_handle(
                    &mut source,
                    RightsOffer::Exact(BYTES),
                )?],
            );
            match sent {
                Ok(()) => {
                    if source.is_some() {
                        return Err("successful MOVE retained source".into());
                    }
                    break;
                }
                Err(Error::Status(Status::FAULT)) if stopping.load(Ordering::Acquire) => {
                    // The rendezvous ABI reports FAULT if receiver admission
                    // closes after output copy but before handle publication.
                    // This is only expected after our explicit shutdown request;
                    // source ownership and eventual endpoint closure still hold.
                    source
                        .as_ref()
                        .ok_or("late rollback consumed source")?
                        .info()?;
                    wait_many(
                        &[WaitItem::new(
                            transfer.as_handle_ref(),
                            ObjectSignals::<CapabilityChannelObject>::PEER_CLOSED,
                        )],
                        deadline()?,
                    )?;
                    return Ok(sequence as usize);
                }
                Err(Error::Status(status))
                    if status == Status::WOULD_BLOCK || status == Status::PEER_CLOSED =>
                {
                    // Both failed publication and peer death must preserve MOVE ownership.
                    source
                        .as_ref()
                        .ok_or("failed MOVE consumed source")?
                        .info()?;
                    if status == Status::PEER_CLOSED {
                        return Ok(sequence as usize);
                    }
                    wait_many(
                        &[WaitItem::new(
                            transfer.as_handle_ref(),
                            ObjectSignals::<CapabilityChannelObject>::PEER_RECEIVING
                                .union(ObjectSignals::<CapabilityChannelObject>::PEER_CLOSED),
                        )],
                        deadline()?,
                    )?;
                }
                Err(error) => {
                    return Err(format!("capability send sequence={sequence}: {error:?}").into());
                }
            }
        }
        let mut echo = [0; 8];
        match receive(&peer, &mut echo) {
            Ok(8) if echo == sequence.to_le_bytes() => {}
            Err(Error::Status(Status::PEER_CLOSED)) => return Ok(sequence as usize),
            other => return Err(format!("transfer echo {sequence}: {other:?}, {echo:?}").into()),
        }
        // Every received owner must be closed by the child, including when its
        // process dies between capability publication and the echo write.
        wait_many(
            &[WaitItem::new(
                peer.as_handle_ref(),
                ObjectSignals::<ByteChannelObject>::PEER_CLOSED,
            )],
            deadline()?,
        )?;
    }
    Err("sequence exhausted".into())
}

fn round(
    startup: &startup::Startup<'_>,
    root: &Directory,
    seed: u64,
    normal_exit: bool,
) -> Result<usize> {
    let (child, control, transfer) = launch(startup, root, seed)?;
    let mut message = [0; 128];
    let n =
        receive(&control, &mut message).map_err(|e| format!("wait for child readiness: {e:?}"))?;
    if &message[..n] != b"ready" {
        return Err(format!("child readiness: {:?}", &message[..n]).into());
    }
    let stopping = Arc::new(AtomicBool::new(false));
    let sender = std::thread::Builder::new()
        .name("stress-sender".into())
        .spawn({
            let stopping = stopping.clone();
            move || send_until_closed(transfer, &stopping)
        })?;
    let outcome = (|| -> Result<()> {
        // A completed transfer plus a completed handle-page churn establishes
        // actual work before termination; timing alone is not a readiness oracle.
        for _ in 0..1 + seed % 7 {
            let n = receive(&control, &mut message)
                .map_err(|e| format!("child progress receive: {e:?}"))?;
            if &message[..n] != b"transfer" {
                return Err(format!("child progress: {:?}", &message[..n]).into());
            }
        }
        for _ in 0..seed % 17 {
            hyper_os::task::yield_now()?;
        }
        stopping.store(true, Ordering::Release);
        if normal_exit {
            control.as_byte_channel().send(b"exit")?;
            let limit = Instant::now() + Duration::from_secs(30);
            loop {
                if Instant::now() >= limit {
                    return Err("cooperative child shutdown timed out".into());
                }
                let n = receive(&control, &mut message)
                    .map_err(|e| format!("drain child shutdown: {e:?}"))?;
                if &message[..n] == b"exiting" {
                    break;
                }
                if &message[..n] != b"transfer" {
                    return Err(format!("child shutdown: {:?}", &message[..n]).into());
                }
            }
        } else {
            child.0.as_process_supervisor().request_stop()?;
        }
        child
            .0
            .as_process_supervisor()
            .wait_terminated(deadline()?)?;
        let reason = child.0.as_process_supervisor().info()?.terminal;
        let correct = if normal_exit {
            matches!(
                reason,
                Some(
                    ProcessTermination::ProcessExited { status: 0 }
                        | ProcessTermination::LastThreadExited { status: 0 }
                )
            )
        } else {
            reason == Some(ProcessTermination::Requested)
        };
        if !correct {
            return Err(format!("unexpected child termination: {reason:?}").into());
        }
        wait_many(
            &[WaitItem::new(
                control.as_handle_ref(),
                ObjectSignals::<ByteChannelObject>::PEER_CLOSED,
            )],
            deadline()?,
        )?;
        Ok(())
    })();
    // On failure close/cancel the peer before joining the sender. No background
    // writer is allowed to survive the command's structured result capture.
    stopping.store(true, Ordering::Release);
    drop(child);
    let sent = sender.join().map_err(|_| "sender panicked")?;
    outcome.map_err(|e| format!("termination: {e}"))?;
    let sent = sent.map_err(|e| format!("sender: {e}"))?;
    if sent == 0 {
        return Err("no completed capability transfer".into());
    }
    Ok(sent)
}

fn churn(seed: u64, stop: &AtomicBool, ready: &AtomicUsize) -> Result<()> {
    let mut iteration = 0;
    while !stop.load(Ordering::Acquire) {
        // Live handles exceed several slot pages. Randomized close order
        // repeatedly creates holes and releases pages while receive publishes.
        let mut handles = Vec::new();
        for _ in 0..65 + seed as usize % 64 {
            let (a, b) = channel::create_pair()?;
            handles.push(a.duplicate(BYTES)?);
            handles.push(a);
            handles.push(b);
        }
        let mut random = seed.wrapping_add(iteration);
        while !handles.is_empty() {
            random = random.wrapping_mul(6364136223846793005).wrapping_add(1);
            let index = random as usize % handles.len();
            handles
                .swap_remove(index)
                .try_close()
                .map_err(|failure| failure.error())?;
        }
        if iteration == 0 {
            ready.fetch_add(1, Ordering::Release);
        }
        iteration += 1;
    }
    Ok(())
}

fn child(seed: u64) -> Result<()> {
    let mut startup = hyper_rt::process::startup()?;
    let control = Arc::new(startup.take(CONTROL)?);
    let transfer = CapabilityChannel::from_handle(startup.take(TRANSFER)?);
    let stop = Arc::new(AtomicBool::new(false));
    let ready = Arc::new(AtomicUsize::new(0));
    let mut workers = Vec::new();
    for id in 0..2 {
        let stop = stop.clone();
        let ready = ready.clone();
        let control = control.clone();
        workers.push(
            std::thread::Builder::new()
                .name(format!("stress-churn-{id}"))
                .spawn(move || worker(&control, || churn(seed.wrapping_add(id), &stop, &ready)))?,
        );
    }
    let (blocked, wake) = channel::create_pair()?;
    let ready_blocked = ready.clone();
    let control_blocked = control.clone();
    workers.push(
        std::thread::Builder::new()
            .name("stress-blocked".into())
            .spawn(move || {
                worker(&control_blocked, || {
                    ready_blocked.fetch_add(1, Ordering::Release);
                    match blocked.as_byte_channel().receive(&mut [0; 1]) {
                        Err(Error::Status(Status::PEER_CLOSED)) => Ok(()),
                        other => Err(format!("blocked peer: {other:?}").into()),
                    }
                })
            })?,
    );
    let stop_rx = stop.clone();
    let control_rx = control.clone();
    workers.push(
        std::thread::Builder::new()
            .name("stress-receiver".into())
            .spawn(move || {
                worker(&control_rx, || {
                    while !stop_rx.load(Ordering::Acquire) {
                        let mut bytes = [MaybeUninit::uninit(); 8];
                        let mut slots = [CapabilityReceiveSlot::new::<ByteChannelObject>(BYTES)];
                        let timeout =
                            hyper_os::time::deadline_after(Duration::from_millis(100))?.as_raw();
                        let message = match transfer.receive(timeout, &mut bytes, &mut slots) {
                            Ok(message) => message,
                            Err(Error::Status(Status::TIMED_OUT)) => continue,
                            Err(error) => return Err(error.into()),
                        };
                        if message.bytes().len() != 8 || message.capability_count() != 1 {
                            return Err("malformed rendezvous".into());
                        }
                        let owner = slots[0]
                            .take::<ByteChannelObject>()?
                            .ok_or("missing received owner")?;
                        owner.as_byte_channel().send(message.bytes())?;
                        drop(owner);
                        control_rx.as_byte_channel().send(b"transfer")?;
                    }
                    Ok(())
                })
            })?,
    );
    let limit = Instant::now() + Duration::from_secs(30);
    while ready.load(Ordering::Acquire) != 3 {
        if Instant::now() >= limit {
            return Err("workers did not become ready".into());
        }
        hyper_os::task::yield_now()?;
    }
    control.as_byte_channel().send(b"ready")?;
    let mut command = [0; 4];
    if receive(&control, &mut command)? != 4 || command != *b"exit" {
        return Err("invalid parent command".into());
    }
    stop.store(true, Ordering::Release);
    drop(wake);
    for worker in workers {
        worker.join().map_err(|_| "child worker panicked")?;
    }
    control.as_byte_channel().send(b"exiting")?;
    Ok(())
}

fn worker(control: &OwnedHandle<ByteChannelObject>, action: impl FnOnce() -> Result<()>) {
    if let Err(error) = action() {
        let report = format!("worker failed: {error}");
        let _ = control.as_byte_channel().try_send(report.as_bytes());
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("trace") && args.len() == 2 {
        return trace::run();
    }
    if args.get(1).map(String::as_str) == Some("child") {
        return child(args.get(2).ok_or("missing child seed")?.parse()?);
    }
    if args.len() != 3 {
        return Err("usage: lifecycle-stress SEED ROUNDS (1..10000)".into());
    }
    let seed: u64 = args[1].parse()?;
    let rounds: usize = args[2].parse()?;
    if !(1..=10_000).contains(&rounds) {
        return Err("rounds must be 1..10000".into());
    }
    let mut startup = hyper_rt::process::startup()?;
    let root = Directory::from_handle(startup.take(startup::ROOT_DIRECTORY)?);
    for index in 0..rounds {
        let current = seed.wrapping_add(index as u64);
        let normal_exit = current % 3 == 2;
        println!("STRESS_BEGIN round={index} seed={current} normal_exit={normal_exit}");
        let sent = round(&startup, &root, current, normal_exit)
            .map_err(|e| format!("round={index} seed={current}: {e}"))?;
        println!("STRESS_PASS round={index} transfers={sent}");
    }
    println!("LIFECYCLE_STRESS_OK seed={seed} rounds={rounds}");
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("LIFECYCLE_STRESS_FAIL: {error}");
        std::process::exit(1);
    }
}
