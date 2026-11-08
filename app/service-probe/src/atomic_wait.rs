// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Process-shared atomic wait uses VMO identity, never a virtual address.

use hyper_os::{
    Error, Status, channel,
    fs::Directory,
    handle::{
        ByteChannelObject, HandleRef, OwnedHandle, ProcessObject, Rights, RightsOffer, VmarObject,
        VmoObject,
    },
    memory::{PAGE_SIZE, SharedAtomicU32Mapping, SnapshotVmo, WritableVmo},
    startup::{self, StartupPurpose},
    task::{ProcessBuilder, ProcessTermination},
};
use hyper_service::process::{self, LoaderKind};
use std::{
    sync::atomic::{AtomicU32, Ordering},
    time::Duration,
};

type Result<T = ()> = std::result::Result<T, Box<dyn std::error::Error>>;
const MEMORY: StartupPurpose<VmoObject> = StartupPurpose::new(0x80ff_0060);
const COORDINATE: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x80ff_0061);

fn deadline(milliseconds: u64) -> hyper_os::Result<u64> {
    Ok(hyper_os::time::deadline_after(Duration::from_millis(milliseconds))?.as_raw())
}

fn map(
    root: HandleRef<'_, VmarObject>,
    vmo: HandleRef<'_, VmoObject>,
) -> Result<SharedAtomicU32Mapping> {
    // SAFETY: root belongs to this process; this test never unmaps/protects the
    // reserved child, and every alias uses aligned AtomicU32 accesses.
    Ok(unsafe { SharedAtomicU32Mapping::map(root, vmo, PAGE_SIZE) }?)
}

fn private_wait(mapping: &SharedAtomicU32Mapping, until: u64) -> hyper_os::Result<()> {
    // SAFETY: the SDK owner keeps this aligned first word mapped until wait
    // returns; all aliases are AtomicU32 and no external VMAR edits occur.
    let word = unsafe { &*std::ptr::with_exposed_provenance::<AtomicU32>(mapping.base_address()) };
    hyper_os::thread::atomic_wait(word, 0, until)
}

struct Child(OwnedHandle<ProcessObject>);
impl Drop for Child {
    fn drop(&mut self) {
        let _ = self.0.as_process_supervisor().request_stop();
        if let Ok(until) = deadline(30_000) {
            let _ = self.0.as_process_supervisor().wait_terminated(until);
        }
    }
}
impl Child {
    fn join(&self, stopped: bool) -> Result {
        self.0
            .as_process_supervisor()
            .wait_terminated(deadline(30_000)?)?;
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
            return Err(format!("atomic wait child failed: {reason:?}").into());
        }
        Ok(())
    }
}
fn send(channel: &OwnedHandle<ByteChannelObject>, bytes: &[u8]) -> Result {
    channel
        .as_byte_channel()
        .send_until(bytes, deadline(30_000)?)?;
    Ok(())
}
fn receive(channel: &OwnedHandle<ByteChannelObject>, expected: &[u8]) -> Result {
    let mut bytes = [0; 32];
    let size = channel
        .as_byte_channel()
        .receive_until(&mut bytes, deadline(30_000)?)?;
    if &bytes[..size] != expected {
        return Err("atomic wait coordination mismatch".into());
    }
    Ok(())
}
fn launch(
    startup: &startup::Startup<'_>,
    root: &Directory,
    vmo: &WritableVmo,
    address: usize,
    mode: &str,
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
    builder.set_name("shared-atomic-child")?;
    for argument in [
        "/bin/service-probe",
        "atomic-wait-child",
        mode,
        &address.to_string(),
    ] {
        builder.add_argument(argument)?;
    }
    let libraries = process::open_library_directory(root)?;
    builder.add_handle_duplicate(
        libraries.as_handle_ref(),
        startup::DYNAMIC_LIBRARY_DIRECTORY.as_raw(),
        RightsOffer::Exact(Rights::READ.union(Rights::EXECUTE).union(Rights::PROTECT)),
    )?;
    builder.add_handle_duplicate(
        vmo.as_handle_ref(),
        MEMORY.as_raw(),
        RightsOffer::Exact(
            Rights::READ
                .union(Rights::WRITE)
                .union(Rights::MAP)
                .union(Rights::INSPECT),
        ),
    )?;
    let (coordinate, child_coordinate) = channel::create_pair()?;
    builder
        .add_handle_move(
            child_coordinate,
            COORDINATE.as_raw(),
            RightsOffer::SameRights,
        )
        .map_err(|failure| failure.error())?;
    builder.seal()?;
    let process = builder
        .start()
        .map_err(|failure| format!("atomic child launch: {:?}", failure.error()))?;
    Ok((Child(process), coordinate))
}

fn check_local(vmo: &WritableVmo) -> Result {
    // SAFETY: the test's page is initialized and all concurrent accesses to it
    // use aligned AtomicU32 words. These calls do not depend on a caller mapping.
    unsafe {
        vmo.wait_u32(0, 1, deadline(100)?)?;
    }
    if vmo.wake_u32(0, 0)? != 0 {
        return Err("zero-count wake returned waiters".into());
    }
    // SAFETY: the same ordinary VMO/atomic access contract; no writer changes 0.
    let timeout = unsafe { vmo.wait_u32(0, 0, deadline(5)?) };
    if !matches!(timeout, Err(Error::Status(Status::TIMED_OUT))) {
        return Err("VMO wait did not time out".into());
    }
    let read_write = vmo
        .as_handle_ref()
        .duplicate(Rights::READ.union(Rights::WRITE))?;
    // SAFETY: this handle names the same initialized atomic word. Its missing
    // MAP right must not prevent VMO-relative wait admission or value checks.
    unsafe {
        read_write.as_handle_ref().wait_u32(0, 1, deadline(100)?)?;
    }
    // SAFETY: the same fixed-width aliases remain live; no writer changes zero.
    let timeout = unsafe { read_write.as_handle_ref().wait_u32(0, 0, deadline(5)?) };
    if !matches!(timeout, Err(Error::Status(Status::TIMED_OUT))) {
        return Err("READ|WRITE without MAP did not admit a shared wait".into());
    }
    let read = vmo.as_handle_ref().duplicate(Rights::READ)?;
    let write = vmo.as_handle_ref().duplicate(Rights::WRITE)?;
    for restricted in [read.as_handle_ref(), write.as_handle_ref()] {
        // SAFETY: rights are deliberately insufficient, but the backing obeys
        // the same aligned atomic access contract as successful invocations.
        let result = unsafe { restricted.wait_u32(0, 1, deadline(100)?) };
        if !matches!(result, Err(Error::Status(Status::ACCESS_DENIED))) {
            return Err("shared wait accepted missing READ or WRITE authority".into());
        }
    }
    if !matches!(
        read.as_handle_ref().wake_u32(0, 1),
        Err(Error::Status(Status::ACCESS_DENIED))
    ) {
        return Err("shared wake accepted missing WRITE authority".into());
    }
    if write.as_handle_ref().wake_u32(0, 1)? != 0 {
        return Err("WRITE-only wake invented a waiter".into());
    }
    for offset in [1, PAGE_SIZE, u64::MAX - 3] {
        // SAFETY: invalid offsets must be rejected before touching backing; the
        // underlying ordinary VMO remains live and only AtomicU32 aliases exist.
        let waited = unsafe { vmo.as_handle_ref().wait_u32(offset, 0, deadline(100)?) };
        let woke = vmo.as_handle_ref().wake_u32(offset, 1);
        let expected = if offset == PAGE_SIZE {
            Error::Status(Status::INVALID_ARGUMENT)
        } else {
            Error::InvalidMemoryRange
        };
        if waited != Err(expected) || woke != Err(expected) {
            return Err(
                format!("shared atomic offset {offset}: wait={waited:?}, wake={woke:?}").into(),
            );
        }
    }
    Ok(())
}

/// Wrong-key wake calls run throughout the child's timed wait. A positive
/// count is a failure even if the child tolerates spurious wakeups correctly.
fn isolation(
    channel: &OwnedHandle<ByteChannelObject>,
    vmo: &WritableVmo,
    other: &WritableVmo,
    private: bool,
) -> Result {
    receive(channel, b"ready")?;
    let until = deadline(30_000)?;
    loop {
        if vmo.wake_u32(4, 1)? != 0
            || other.wake_u32(0, 1)? != 0
            || vmo.wake_u32(0, 0)? != 0
            || (private && vmo.wake_u32(0, 1)? != 0)
        {
            return Err("shared atomic wake crossed object/offset/private namespace".into());
        }
        let mut bytes = [0; 32];
        match channel.as_byte_channel().try_receive(&mut bytes) {
            Ok(size) if &bytes[..size] == b"isolated" => return Ok(()),
            Ok(_) => return Err("isolation completion mismatch".into()),
            Err(Error::Status(Status::WOULD_BLOCK)) => {}
            Err(error) => return Err(error.into()),
        }
        if hyper_os::time::monotonic_now()?.as_nanoseconds() >= until {
            return Err("isolation child stalled".into());
        }
        hyper_os::task::yield_now()?;
    }
}

pub(super) fn verify() -> Result {
    let mut startup = hyper_rt::process::startup()?;
    let root = startup.take_root_directory()?;
    let root_vmar = startup.borrow(startup::ROOT_VMAR)?;
    let vmo = WritableVmo::create(PAGE_SIZE)?;
    let other = WritableVmo::create(PAGE_SIZE)?;
    // Populate before publishing either atomic mapping, with no concurrent aliases.
    vmo.write_all_at(0, &[0; 8])?;
    other.write_all_at(0, &[0; 8])?;
    let mapping = map(root_vmar, vmo.as_handle_ref())?;
    check_local(&vmo)?;
    for mode in ["shared", "private", "stop"] {
        mapping.store(0, 0, Ordering::SeqCst)?;
        let (child, coordinate) = launch(&startup, &root, &vmo, mapping.base_address(), mode)?;
        receive(&coordinate, b"different-address")?;
        send(&coordinate, b"go")?;
        if mode == "stop" {
            receive(&coordinate, b"ready")?;
            // Exercise cancellation whether stop races registration or finds a
            // parked waiter. Process termination must retire either path.
            child.0.as_process_supervisor().request_stop()?;
            child.join(true)?;
        } else {
            isolation(&coordinate, &vmo, &other, mode == "private")?;
            if mode == "shared" {
                send(&coordinate, b"park")?;
                receive(&coordinate, b"ready")?;
                let until = deadline(30_000)?;
                loop {
                    if vmo.wake_u32(0, 1)? == 1 {
                        break;
                    }
                    if hyper_os::time::monotonic_now()?.as_nanoseconds() >= until {
                        return Err("same-VMO wake never matched child".into());
                    }
                    hyper_os::task::yield_now()?;
                }
                mapping.store(0, 99, Ordering::Release)?;
                send(&coordinate, b"published")?;
            }
            receive(&coordinate, b"done")?;
            child.join(false)?;
        }
        if vmo.wake_u32(0, u32::MAX)? != 0 {
            return Err("terminated child leaked a shared waiter".into());
        }
    }
    drop(mapping);
    // A stopped wait must not retain the snapshot-excluding writable lease.
    let until = deadline(3_000)?;
    loop {
        match SnapshotVmo::from_vmo(vmo.as_handle_ref()) {
            Ok(_) => break,
            Err(Error::Status(Status::BUSY))
                if hyper_os::time::monotonic_now()?.as_nanoseconds() < until =>
            {
                hyper_os::task::yield_now()?
            }
            Err(error) => return Err(error.into()),
        }
    }
    println!("VMO_ATOMIC_WAIT_CROSS_PROCESS_OK");
    Ok(())
}

pub(super) fn child() -> Result {
    let mode = std::env::args().nth(2).ok_or("missing atomic wait mode")?;
    let parent_address: usize = std::env::args()
        .nth(3)
        .ok_or("missing parent address")?
        .parse()?;
    let mut startup = hyper_rt::process::startup()?;
    let vmo = startup.take(MEMORY)?;
    let coordinate = startup.take(COORDINATE)?;
    let root = startup.borrow(startup::ROOT_VMAR)?;
    let first = map(root, vmo.as_handle_ref())?;
    // Retain a colliding first reservation so the second cannot reuse its VA.
    let second = if first.base_address() == parent_address {
        Some(map(root, vmo.as_handle_ref())?)
    } else {
        None
    };
    let mapping = second.as_ref().unwrap_or(&first);
    if mapping.base_address() == parent_address {
        return Err("shared mapping addresses unexpectedly match".into());
    }
    send(&coordinate, b"different-address")?;
    receive(&coordinate, b"go")?;
    send(&coordinate, b"ready")?;
    if mode == "stop" {
        let until = deadline(30_000)?;
        loop {
            // SAFETY: every alias is an aligned AtomicU32 under retained mappings.
            unsafe {
                vmo.as_handle_ref().wait_u32(0, 0, until)?;
            }
        }
    }
    let until = deadline(100)?;
    loop {
        let result = if mode == "private" {
            private_wait(mapping, until)
        } else if mode == "shared" {
            // SAFETY: initialized ordinary VMO, live fixed-width atomic aliases.
            unsafe { vmo.as_handle_ref().wait_u32(0, 0, until) }
        } else {
            return Err("unknown atomic wait mode".into());
        };
        match result {
            Ok(()) => {} // The wait API permits spurious wakeups.
            Err(Error::Status(Status::TIMED_OUT)) => break,
            Err(error) => return Err(error.into()),
        }
    }
    send(&coordinate, b"isolated")?;
    if mode == "shared" {
        receive(&coordinate, b"park")?;
        send(&coordinate, b"ready")?;
        let until = deadline(30_000)?;
        loop {
            // SAFETY: same shared word; parent requires an actual matched wake
            // count. Short sub-deadlines let a spurious wake race its control reply.
            match unsafe {
                vmo.as_handle_ref()
                    .wait_u32(0, 0, deadline(100)?.min(until))
            } {
                Ok(()) | Err(Error::Status(Status::TIMED_OUT)) => {}
                Err(error) => return Err(error.into()),
            }
            let mut bytes = [0; 32];
            match coordinate.as_byte_channel().try_receive(&mut bytes) {
                Ok(size) if &bytes[..size] == b"published" => break,
                Ok(_) => return Err("shared wake publication reply mismatch".into()),
                Err(Error::Status(Status::WOULD_BLOCK)) => {}
                Err(error) => return Err(error.into()),
            }
            if hyper_os::time::monotonic_now()?.as_nanoseconds() >= until {
                return Err("shared wake publication stalled".into());
            }
        }
        if mapping.load(0, Ordering::Acquire)? != 99 {
            return Err("different-address mapping missed shared value".into());
        }
    }
    send(&coordinate, b"done")?;
    Ok(())
}
