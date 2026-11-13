// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Real process/thread death, cancellation and shared recovery qualification.

use hyper_os::{
    Error, Status, channel,
    fs::Directory,
    handle::{
        ByteChannelObject, OwnedHandle, ProcessObject, Rights, RightsOffer, SyncDomainObject,
        VmoObject,
    },
    memory::{PAGE_SIZE, SharedAtomicU32Mapping, WritableVmo},
    startup::{self, StartupPurpose},
    sync::{Acquisition, Cancellation, LockError, OwnerState, SharedMutexRegion, SyncDomain},
    task::{ProcessBuilder, ProcessTermination},
};
use hyper_service::process::{self, LoaderKind};
use std::{sync::atomic::Ordering, time::Duration};

type Result<T = ()> = std::result::Result<T, Box<dyn std::error::Error>>;

trait At<T> {
    fn at(self, operation: &str) -> Result<T>;
}
impl<T, E: std::fmt::Display> At<T> for std::result::Result<T, E> {
    fn at(self, operation: &str) -> Result<T> {
        self.map_err(|error| format!("shared mutex {operation}: {error}").into())
    }
}

const DOMAIN: StartupPurpose<SyncDomainObject> = StartupPurpose::new(0x80ff_0070);
const MEMORY: StartupPurpose<VmoObject> = StartupPurpose::new(0x80ff_0071);
const DATA: StartupPurpose<VmoObject> = StartupPurpose::new(0x80ff_0072);
const COORDINATE: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x80ff_0073);
const PAYLOAD: u32 = 0x71de_a761;
const FINAL_PAYLOAD: u32 = 0x91bc_e462;

fn deadline(milliseconds: u64) -> hyper_os::Result<u64> {
    Ok(hyper_os::time::deadline_after(Duration::from_millis(milliseconds))?.as_raw())
}
fn send(channel: &OwnedHandle<ByteChannelObject>, bytes: &[u8]) -> Result {
    channel
        .as_byte_channel()
        .send_until(bytes, deadline(30_000)?)?;
    Ok(())
}
fn receive(channel: &OwnedHandle<ByteChannelObject>) -> Result<Vec<u8>> {
    let mut bytes = [0; 32];
    let size = channel
        .as_byte_channel()
        .receive_until(&mut bytes, deadline(30_000)?)?;
    Ok(bytes[..size].to_vec())
}
fn ready(channel: &OwnedHandle<ByteChannelObject>) -> Result {
    if receive(channel)? != b"owned" {
        return Err("mutex owner did not acquire".into());
    }
    Ok(())
}
struct Child(OwnedHandle<ProcessObject>);
impl Child {
    fn join(&self, requested: bool) -> Result {
        self.0
            .as_process_supervisor()
            .wait_terminated(deadline(30_000)?)?;
        let reason = self.0.as_process_supervisor().info()?.terminal;
        let valid = if requested {
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
            return Err(format!("mutex child failed: {reason:?}").into());
        }
        Ok(())
    }
}
impl Drop for Child {
    fn drop(&mut self) {
        let _ = self.0.as_process_supervisor().request_stop();
        if let Ok(until) = deadline(30_000) {
            let _ = self.0.as_process_supervisor().wait_terminated(until);
        }
    }
}
fn launch(
    startup: &startup::Startup<'_>,
    root: &Directory,
    domain: &SyncDomain,
    memory: &WritableVmo,
    data: &WritableVmo,
    mode: &str,
    index: usize,
) -> Result<(Child, OwnedHandle<ByteChannelObject>)> {
    let image = root
        .open("/bin/service-probe", process::EXECUTABLE_FILE_RIGHTS)
        .at("open child image")?;
    let loader = process::open_loader(root, LoaderKind::Dynamic).at("open child loader")?;
    let builder = ProcessBuilder::create(
        startup.borrow(startup::TASK_FACTORY)?,
        startup.borrow(startup::TASK_GROUP)?,
        startup.borrow(startup::RESOURCE_DOMAIN)?,
        image.as_handle_ref(),
        loader.as_handle_ref(),
    )
    .at("create child builder")?;
    builder
        .set_name("shared-mutex-child")
        .at("set child name")?;
    for argument in [
        "/bin/service-probe",
        "shared-mutex-child",
        mode,
        &index.to_string(),
    ] {
        builder.add_argument(argument).at("set child argument")?;
    }
    let libraries = process::open_library_directory(root).at("open child libraries")?;
    builder
        .add_handle_duplicate(
            libraries.as_handle_ref(),
            startup::DYNAMIC_LIBRARY_DIRECTORY.as_raw(),
            RightsOffer::Exact(Rights::READ.union(Rights::EXECUTE).union(Rights::PROTECT)),
        )
        .at("delegate child library directory")?;
    builder
        .add_handle_duplicate(
            domain.as_handle_ref(),
            DOMAIN.as_raw(),
            RightsOffer::Exact(
                Rights::SYNC_WAIT
                    .union(Rights::SYNC_WAKE)
                    .union(Rights::INSPECT)
                    .union(Rights::SYNC_JOIN),
            ),
        )
        .at("delegate child domain")?;
    for (vmo, purpose) in [(memory, MEMORY), (data, DATA)] {
        builder
            .add_handle_duplicate(
                vmo.as_handle_ref(),
                purpose.as_raw(),
                RightsOffer::Exact(
                    Rights::READ
                        .union(Rights::WRITE)
                        .union(Rights::MAP)
                        .union(Rights::INSPECT),
                ),
            )
            .at("delegate child VMO")?;
    }
    let (coordinate, child_coordinate) = channel::create_pair()?;
    builder
        .add_handle_move(
            child_coordinate,
            COORDINATE.as_raw(),
            RightsOffer::SameRights,
        )
        .map_err(|failure| failure.error())?;
    builder.seal().at("seal child")?;
    let process = builder
        .start()
        .map_err(|failure| format!("mutex child launch: {:?}", failure.error()))?;
    Ok((Child(process), coordinate))
}

pub(super) fn verify() -> Result {
    let mut startup = hyper_rt::process::startup().at("read startup")?;
    let root = startup.take_root_directory().at("take root directory")?;
    let root_vmar = startup.borrow(startup::ROOT_VMAR).at("borrow root VMAR")?;
    let memory = WritableVmo::create(PAGE_SIZE).at("create control VMO")?;
    let data = WritableVmo::create(PAGE_SIZE).at("create payload VMO")?;
    let domain = SyncDomain::create().at("create domain")?;
    domain
        .bind(memory.as_handle_ref(), 0, PAGE_SIZE)
        .at("bind control page")?;
    domain
        .bind(memory.as_handle_ref(), 8, 8)
        .at("bind overlapping subrange")?;
    let conflict = SyncDomain::create().at("create conflicting domain")?;
    if conflict.bind(memory.as_handle_ref(), 0, 8).is_ok() {
        return Err("conflicting synchronization domains accepted".into());
    }
    let unprivileged = SyncDomain::from_handle(
        domain.as_handle_ref().duplicate(
            Rights::SYNC_WAIT
                .union(Rights::SYNC_WAKE)
                .union(Rights::INSPECT)
                .union(Rights::SYNC_JOIN),
        )?,
    )
    .at("import attenuated domain")?;
    if unprivileged.bind(memory.as_handle_ref(), 16, 8) != Err(Error::Status(Status::ACCESS_DENIED))
    {
        return Err("domain binding accepted missing configure authority".into());
    }
    let participant = domain.join().at("join domain")?;
    if domain.join().at("repeat join")?.token() != participant.token()
        || domain.owner_query(0).at("query zero owner")? != OwnerState::Invalid
        || domain
            .owner_query(u64::MAX)
            .at("query out-of-range owner")?
            != OwnerState::Invalid
    {
        return Err("participant identity or invalid-token contract failed".into());
    }
    // SAFETY: fresh ordinary RAM has zero control words; every process uses this
    // same u64 mutex protocol and retains its private VMAR throughout all guards.
    let region =
        unsafe { SharedMutexRegion::map(&domain, root_vmar, memory.as_handle_ref(), 0, PAGE_SIZE) }
            .at("map control region")?;
    // SAFETY: data is a separate VMO, only accessed with aligned u32 atomics.
    let payload =
        unsafe { SharedAtomicU32Mapping::map(root_vmar, data.as_handle_ref(), PAGE_SIZE) }
            .at("map payload region")?;
    let mutex = region.mutex(0).at("open first mutex view")?;
    for _ in 0..1000 {
        match mutex.try_lock(&participant)? {
            Acquisition::Acquired(guard) => guard.unlock(),
            Acquisition::OwnerDied(_) => return Err("fresh mutex reported dead owner".into()),
        }
    }
    // A distinct local domain wrapper and simultaneous VMAR alias must retain
    // the same lock identity without requiring the same process address.
    // SAFETY: this maps the same initialized u64 protocol through a second
    // private child, which remains retained until all derived guards end.
    let alias = unsafe {
        SharedMutexRegion::map(
            &unprivileged,
            root_vmar,
            memory.as_handle_ref(),
            0,
            PAGE_SIZE,
        )
    }
    .at("map alias region")?;
    let alias_mutex = alias.mutex(0).at("open alias mutex view")?;
    let held = mutex.try_lock(&participant)?;
    if !matches!(
        alias_mutex.try_lock(&participant),
        Err(LockError::AlreadyOwned)
    ) {
        return Err("same-domain alias changed owner identity".into());
    }
    drop(held);
    match alias_mutex.try_lock(&participant)? {
        Acquisition::Acquired(guard) => guard.unlock(),
        Acquisition::OwnerDied(_) => return Err("same-domain alias invented owner death".into()),
    }
    let (owner, coordinate) = launch(&startup, &root, &domain, &memory, &data, "owner", 0)?;
    ready(&coordinate)?;
    if !matches!(mutex.try_lock(&participant), Err(LockError::WouldBlock)) {
        return Err("live owner was stolen".into());
    }
    if !matches!(
        mutex.lock_until(&participant, deadline(5)?, None),
        Err(LockError::TimedOut)
    ) {
        return Err("contended timed lock did not time out".into());
    }
    let cancellation = Cancellation::new().at("create cancellation Event")?;
    std::thread::scope(|scope| -> Result {
        let notifier = scope.spawn(|| {
            std::thread::sleep(Duration::from_millis(20));
            cancellation.cancel()
        });
        let acquired = mutex.lock_until(&participant, deadline(5_000)?, Some(&cancellation));
        notifier
            .join()
            .map_err(|_| "cancellation worker panicked")??;
        if !matches!(acquired, Err(LockError::Cancelled)) {
            return Err("parked shared mutex acquisition did not cancel".into());
        }
        Ok(())
    })?;
    send(&coordinate, b"die")?;
    match mutex.lock_until(&participant, deadline(10_000)?, None)? {
        Acquisition::OwnerDied(recovery) => {
            if payload.load(0, Ordering::Relaxed)? != FINAL_PAYLOAD {
                return Err("death observation lost completed payload writes".into());
            }
            recovery
                .mark_consistent()
                .map_err(|failure| failure.error)?
                .unlock();
        }
        Acquisition::Acquired(_) => return Err("owner death was reported as normal release".into()),
    }
    owner.join(false)?;
    // Normal release stays distinct from process death after that release.
    let (owner, coordinate) = launch(&startup, &root, &domain, &memory, &data, "owner", 0)?;
    ready(&coordinate)?;
    send(&coordinate, b"release")?;
    match mutex.lock_until(&participant, deadline(10_000)?, None)? {
        Acquisition::Acquired(guard) => guard.unlock(),
        Acquisition::OwnerDied(_) => return Err("normal unlock falsely poisoned the mutex".into()),
    }
    owner.join(false)?;
    // Recovery owner death must grant recovery again, not mark data consistent.
    let mutex = region.mutex(1).at("open recovery mutex view")?;
    let (owner, coordinate) = launch(&startup, &root, &domain, &memory, &data, "owner", 1)?;
    ready(&coordinate)?;
    send(&coordinate, b"die")?;
    owner.join(false)?;
    let (repairer, coordinate) = launch(&startup, &root, &domain, &memory, &data, "recover", 1)?;
    ready(&coordinate)?;
    repairer.0.as_process_supervisor().request_stop()?;
    repairer.join(true)?;
    match mutex.lock_until(&participant, deadline(10_000)?, None)? {
        Acquisition::OwnerDied(recovery) => recovery.abandon(),
        Acquisition::Acquired(_) => return Err("dead recovery owner became consistent".into()),
    }
    if !matches!(mutex.try_lock(&participant), Err(LockError::NotRecoverable)) {
        return Err("abandoned recovery was not permanent".into());
    }
    // A single Thread exit while its Process survives has identical semantics.
    let mutex = region.mutex(2).at("open Thread mutex view")?;
    let token = std::thread::scope(|scope| {
        scope
            .spawn(|| -> hyper_os::Result<u64> {
                let joined = domain.join()?;
                let held = mutex.lock(&joined).map_err(|_| Error::InvalidResponse)?;
                let token = joined.token();
                core::mem::forget(held);
                Ok(token)
            })
            .join()
    })
    .map_err(|_| "mutex worker panicked")??;
    if domain.owner_query(token)? != OwnerState::Dead {
        return Err("Thread exit failed to publish owner death".into());
    }
    match mutex.lock_until(&participant, deadline(10_000)?, None)? {
        Acquisition::OwnerDied(recovery) => recovery
            .mark_consistent()
            .map_err(|failure| failure.error)?
            .unlock(),
        Acquisition::Acquired(_) => return Err("Thread death lost recovery status".into()),
    }
    println!("SHARED_MUTEX_OWNER_DEATH_CROSS_PROCESS_OK");
    Ok(())
}

pub(super) fn child() -> Result {
    let mode = std::env::args().nth(2).ok_or("missing shared mutex mode")?;
    let index: usize = std::env::args()
        .nth(3)
        .ok_or("missing mutex index")?
        .parse()?;
    let mut startup = hyper_rt::process::startup().at("read startup")?;
    let domain = SyncDomain::from_handle(startup.take(DOMAIN)?).at("child import domain")?;
    let memory = startup.take(MEMORY)?;
    let data = startup.take(DATA)?;
    let coordinate = startup.take(COORDINATE)?;
    let root = startup.borrow(startup::ROOT_VMAR)?;
    let participant = domain.join().at("join domain")?;
    // SAFETY: parent bound this initialized u64-control VMO and all aliases obey
    // the same protocol; the child exclusively owns its reserved VMAR mapping.
    let region =
        unsafe { SharedMutexRegion::map(&domain, root, memory.as_handle_ref(), 0, PAGE_SIZE) }?;
    // SAFETY: this distinct data VMO contains only aligned u32 atomic payload.
    let payload = unsafe { SharedAtomicU32Mapping::map(root, data.as_handle_ref(), PAGE_SIZE) }?;
    let mutex = region.mutex(index).at("child open mutex view")?;
    let held = mutex.lock_until(&participant, deadline(10_000)?, None)?;
    if !matches!(
        (&held, mode.as_str()),
        (Acquisition::Acquired(_), "owner") | (Acquisition::OwnerDied(_), "recover")
    ) {
        return Err("child acquired unexpected ownership state".into());
    }
    payload.store(0, PAYLOAD, Ordering::Relaxed)?;
    send(&coordinate, b"owned")?;
    match receive(&coordinate)?.as_slice() {
        b"die" => {
            // No channel publication follows this write. Recovery must observe
            // it through Thread death, not the earlier "owned" handshake.
            payload.store(0, FINAL_PAYLOAD, Ordering::Relaxed)?;
            core::mem::forget(held);
        }
        b"release" if mode == "owner" => drop(held),
        _ => return Err("unknown mutex owner instruction".into()),
    }
    Ok(())
}
