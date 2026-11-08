// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Cross-process shared queues using only explicitly transferred capabilities.

use hyper_os::{
    channel,
    event::Event,
    fs::Directory,
    handle::{
        ByteChannelObject, EventObject, OwnedHandle, ProcessObject, RightsOffer, TypedObject,
        VmoObject,
    },
    shared_queue::{
        self, Config, Consumer, ConsumerParts, Producer, ProducerParts, QueueError, ReceiveSlot,
    },
    startup::{self, StartupPurpose},
    task::{ProcessBuilder, ProcessTermination},
};
use hyper_service::process::{self, LoaderKind};
use std::time::Duration;

type Result<T = ()> = std::result::Result<T, Box<dyn std::error::Error>>;
const DATA: StartupPurpose<VmoObject> = StartupPurpose::new(0x80ff_0050);
const CONTROL: StartupPurpose<VmoObject> = StartupPurpose::new(0x80ff_0051);
const DATA_READY: StartupPurpose<EventObject> = StartupPurpose::new(0x80ff_0052);
const SPACE_READY: StartupPurpose<EventObject> = StartupPurpose::new(0x80ff_0053);
const LIFETIME: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x80ff_0054);
const COORDINATE: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x80ff_0055);
const MESSAGES: usize = 257;

fn q<T>(result: std::result::Result<T, QueueError>) -> Result<T> {
    result.map_err(|error| format!("shared queue: {error:?}").into())
}

fn config() -> Result<Config> {
    q(Config::new(2, 64))
}
fn deadline() -> hyper_os::Result<u64> {
    Ok(hyper_os::time::deadline_after(Duration::from_secs(30))?.as_raw())
}

struct Child(OwnedHandle<ProcessObject>);
impl Drop for Child {
    fn drop(&mut self) {
        let _ = self.0.as_process_supervisor().request_stop();
        if let Ok(limit) = deadline() {
            let _ = self.0.as_process_supervisor().wait_terminated(limit);
        }
    }
}
impl Child {
    fn joined(&self, stopped: bool) -> Result {
        self.0
            .as_process_supervisor()
            .wait_terminated(deadline()?)?;
        let reason = self.0.as_process_supervisor().info()?.terminal;
        let valid = if stopped {
            reason == Some(ProcessTermination::Requested)
        } else {
            matches!(
                reason,
                Some(
                    ProcessTermination::ProcessExited { status: 0 }
                        | ProcessTermination::LastThreadExited { status: 0 }
                )
            )
        };
        if !valid {
            return Err(format!("shared queue child failed: {reason:?}").into());
        }
        Ok(())
    }
}

enum Parts {
    Producer(ProducerParts),
    Consumer(ConsumerParts),
}
fn install<T: TypedObject>(
    builder: &ProcessBuilder,
    handle: OwnedHandle<T>,
    purpose: StartupPurpose<T>,
) -> hyper_os::Result<()> {
    builder
        .add_handle_move(handle, purpose.as_raw(), RightsOffer::SameRights)
        .map_err(|failure| failure.error())
}

fn launch(
    startup: &startup::Startup<'_>,
    root: &Directory,
    mode: &str,
    parts: Parts,
) -> Result<(Child, OwnedHandle<ByteChannelObject>)> {
    let image = root.open("/bin/service-probe", process::EXECUTABLE_FILE_RIGHTS)?;
    let loader = process::open_loader(root, LoaderKind::Dynamic)?;
    let builder = ProcessBuilder::create(
        startup.borrow(startup::TASK_FACTORY)?,
        startup.borrow(startup::TASK_GROUP)?,
        startup.borrow(startup::RESOURCE_DOMAIN)?,
        image.as_handle_ref(),
        loader.as_handle_ref(),
    )?;
    builder.set_name("shared-queue-child")?;
    for arg in ["/bin/service-probe", "shared-queue-child", mode] {
        builder.add_argument(arg)?;
    }
    let libraries = process::open_library_directory(root)?;
    builder.add_handle_duplicate(
        libraries.as_handle_ref(),
        startup::DYNAMIC_LIBRARY_DIRECTORY.as_raw(),
        RightsOffer::Exact(
            hyper_os::Rights::READ
                .union(hyper_os::Rights::EXECUTE)
                .union(hyper_os::Rights::PROTECT),
        ),
    )?;
    let (data, control, data_ready, space_ready, lifetime) = match parts {
        Parts::Producer(p) => (p.data, p.control, p.data_ready, p.space_ready, p.lifetime),
        Parts::Consumer(p) => (p.data, p.control, p.data_ready, p.space_ready, p.lifetime),
    };
    install(&builder, data, DATA)?;
    install(&builder, control, CONTROL)?;
    install(&builder, data_ready.into_handle(), DATA_READY)?;
    install(&builder, space_ready.into_handle(), SPACE_READY)?;
    install(&builder, lifetime, LIFETIME)?;
    let (coordinate, child_coordinate) = channel::create_pair()?;
    install(&builder, child_coordinate, COORDINATE)?;
    builder.seal()?;
    let child = Child(
        builder
            .start()
            .map_err(|failure| format!("start queue child: {:?}", failure.error()))?,
    );
    Ok((child, coordinate))
}

fn send_control(channel: &OwnedHandle<ByteChannelObject>, message: &[u8]) -> Result {
    channel.as_byte_channel().send_until(message, deadline()?)?;
    Ok(())
}
fn receive_control(channel: &OwnedHandle<ByteChannelObject>, expected: &[u8]) -> Result {
    let mut bytes = [0; 32];
    let count = channel
        .as_byte_channel()
        .receive_until(&mut bytes, deadline()?)?;
    if &bytes[..count] != expected {
        return Err("shared queue coordination mismatch".into());
    }
    Ok(())
}
fn payload(sequence: usize) -> Vec<u8> {
    let mut bytes = vec![sequence as u8; 17 + sequence % 31];
    bytes[..8].copy_from_slice(&(sequence as u64).to_le_bytes());
    bytes
}
fn send_messages(producer: &mut Producer, mut next: usize) -> Result {
    while next < MESSAGES {
        let count = (MESSAGES - next).min(3);
        let bytes: Vec<_> = (next..next + count).map(payload).collect();
        let refs: Vec<_> = bytes.iter().map(Vec::as_slice).collect();
        let result = producer.try_send_batch(&refs);
        if result.completed > count {
            return Err("queue send count exceeded batch".into());
        }
        next += result.completed;
        match result.stop {
            Some(QueueError::Full) => q(producer.wait_writable_until(deadline()?))?,
            Some(error) => return Err(format!("queue send: {error:?}").into()),
            None if result.completed == 0 => return Err("queue send made no progress".into()),
            None => {}
        }
    }
    Ok(())
}
fn receive_messages(consumer: &mut Consumer, expected: usize, abrupt: bool) -> Result {
    let mut next = 0;
    loop {
        let mut first = [0; 64];
        let mut second = [0; 64];
        let mut slots = [ReceiveSlot::new(&mut first), ReceiveSlot::new(&mut second)];
        let result = consumer.try_receive_batch(&mut slots);
        if result.completed > slots.len() {
            return Err("queue receive count exceeded batch".into());
        }
        for slot in &slots[..result.completed] {
            if slot.message() != Some(payload(next).as_slice()) {
                return Err(format!("queue payload mismatch at {next}").into());
            }
            next += 1;
            if next > expected {
                return Err("queue delivered extra messages".into());
            }
        }
        match result.stop {
            Some(QueueError::Closed) if !abrupt && next == expected => return Ok(()),
            Some(QueueError::PeerDied) if abrupt && next == expected => return Ok(()),
            Some(QueueError::Empty) | None => match consumer.wait_readable_until(deadline()?) {
                Ok(()) => {}
                Err(QueueError::Closed) if !abrupt && next == expected => return Ok(()),
                Err(QueueError::PeerDied) if abrupt && next == expected => return Ok(()),
                Err(error) => {
                    return Err(format!("queue read wait: {error:?}, received={next}").into());
                }
            },
            Some(error) => return Err(format!("queue receive: {error:?}, received={next}").into()),
        }
    }
}

pub(super) fn verify() -> Result {
    let mut startup = hyper_rt::process::startup()?;
    let root = startup.take_root_directory()?;
    let vmar = startup.borrow(startup::ROOT_VMAR)?;
    let (producer, consumer) = shared_queue::create_pair(config()?)?;
    // SAFETY: this process owns root, never modifies the reserved child VMARs,
    // and gives all shared aliases exclusively to the queue's atomic protocol.
    let mut producer = q(unsafe { Producer::map(producer, vmar) })?;
    let (child, control) = launch(&startup, &root, "consumer", Parts::Consumer(consumer))?;
    let initial = [payload(0), payload(1), payload(2)];
    let outcome = producer.try_send_batch(&[&initial[0], &initial[1], &initial[2]]);
    if outcome.completed != 2 || !matches!(outcome.stop, Some(QueueError::Full)) {
        return Err("gated consumer did not leave exactly two accepted messages".into());
    }
    // The gated consumer cannot free space. A future deadline forces the
    // Event/WaitSet arm, timeout and disarm path before this owner is reused.
    let timeout = hyper_os::time::deadline_after(Duration::from_millis(5))?.as_raw();
    if !matches!(
        producer.wait_writable_until(timeout),
        Err(QueueError::System(hyper_os::Error::Status(
            hyper_os::Status::TIMED_OUT
        )))
    ) {
        return Err("full queue did not honor its absolute deadline".into());
    }
    send_control(&control, b"go")?;
    send_messages(&mut producer, 2)?;
    q(producer.finish())?;
    receive_control(&control, b"done")?;
    child.joined(false)?;

    for mode in ["producer", "abort-producer"] {
        let (producer, consumer) = shared_queue::create_pair(config()?)?;
        // SAFETY: fresh private mapping owners and the same fixed-width atomic protocol.
        let mut consumer = q(unsafe { Consumer::map(consumer, vmar) })?;
        let (child, control) = launch(&startup, &root, mode, Parts::Producer(producer))?;
        let mut empty = [0; 64];
        let outcome = consumer.try_receive_batch(&mut [ReceiveSlot::new(&mut empty)]);
        if outcome.completed != 0 || !matches!(outcome.stop, Some(QueueError::Empty)) {
            return Err("gated producer did not leave the queue empty".into());
        }
        let timeout = hyper_os::time::deadline_after(Duration::from_millis(5))?.as_raw();
        if !matches!(
            consumer.wait_readable_until(timeout),
            Err(QueueError::System(hyper_os::Error::Status(
                hyper_os::Status::TIMED_OUT
            )))
        ) {
            return Err("empty queue did not honor its absolute deadline".into());
        }
        send_control(&control, b"go")?;
        let abrupt = mode == "abort-producer";
        if abrupt {
            receive_control(&control, b"published")?;
            child.0.as_process_supervisor().request_stop()?;
            child.joined(true)?;
        }
        receive_messages(&mut consumer, if abrupt { 2 } else { MESSAGES }, abrupt)?;
        if !abrupt {
            child.joined(false)?;
        }
    }

    let (producer, consumer) = shared_queue::create_pair(config()?)?;
    // SAFETY: only this producer and the child's consumer access the fresh queue.
    let mut producer = q(unsafe { Producer::map(producer, vmar) })?;
    let (child, control) = launch(&startup, &root, "abort-consumer", Parts::Consumer(consumer))?;
    send_control(&control, b"go")?;
    receive_control(&control, b"mapped")?;
    let first = payload(0);
    let second = payload(1);
    let outcome = producer.try_send_batch(&[&first, &second, &first]);
    if outcome.completed != 2 || !matches!(outcome.stop, Some(QueueError::Full)) {
        return Err("inactive consumer did not leave a full queue".into());
    }
    child.0.as_process_supervisor().request_stop()?;
    child.joined(true)?;
    if !matches!(
        producer.wait_writable_until(deadline()?),
        Err(QueueError::PeerDied)
    ) {
        return Err("dead consumer was not reported to producer".into());
    }
    println!("SHARED_QUEUE_CROSS_PROCESS_OK");
    Ok(())
}

pub(super) fn child() -> Result {
    let mode = std::env::args().nth(2).ok_or("missing queue child mode")?;
    let mut startup = hyper_rt::process::startup()?;
    let data = startup.take(DATA)?;
    let control = startup.take(CONTROL)?;
    let data_ready = Event::from_handle(startup.take(DATA_READY)?);
    let space_ready = Event::from_handle(startup.take(SPACE_READY)?);
    let lifetime = startup.take(LIFETIME)?;
    let coordinate = startup.take(COORDINATE)?;
    let root = startup.borrow(startup::ROOT_VMAR)?;
    receive_control(&coordinate, b"go")?;
    if mode == "consumer" || mode == "abort-consumer" {
        let parts = ConsumerParts {
            config: config()?,
            data,
            control,
            data_ready,
            space_ready,
            lifetime,
        };
        // SAFETY: these moved role capabilities have no other consumer alias;
        // the private VMAR and all shared accesses remain owned by this queue.
        let mut consumer = q(unsafe { Consumer::map(parts, root) })?;
        if mode == "abort-consumer" {
            send_control(&coordinate, b"mapped")?;
            receive_control(&coordinate, b"never")?;
        } else {
            let mut short = [0; 8];
            let outcome = consumer.try_receive_batch(&mut [ReceiveSlot::new(&mut short)]);
            if outcome.completed != 0
                || !matches!(
                    outcome.stop,
                    Some(QueueError::BufferTooSmall { required: 17 })
                )
            {
                return Err("small receive buffer consumed or hid the first message".into());
            }
            receive_messages(&mut consumer, MESSAGES, false)?;
            send_control(&coordinate, b"done")?;
        }
    } else {
        let parts = ProducerParts {
            config: config()?,
            data,
            control,
            data_ready,
            space_ready,
            lifetime,
        };
        // SAFETY: moved producer ownership is unique and every shared access
        // uses the queue's fixed-width atomics under its validated geometry.
        let mut producer = q(unsafe { Producer::map(parts, root) })?;
        if mode == "abort-producer" {
            let first = payload(0);
            let second = payload(1);
            let result = producer.try_send_batch(&[&first, &second]);
            if result.completed != 2 || result.stop.is_some() {
                return Err("abrupt producer failed publication".into());
            }
            send_control(&coordinate, b"published")?;
            receive_control(&coordinate, b"never")?;
        } else if mode == "producer" {
            send_messages(&mut producer, 0)?;
            q(producer.finish())?;
        } else {
            return Err("invalid queue child mode".into());
        }
    }
    Ok(())
}
