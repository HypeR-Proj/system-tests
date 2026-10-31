// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Test-only storage owners: transfer a live channel without VM/device metadata.
//! An independent RAM root is prepared by native-init; this is not a block driver.

use core::mem::MaybeUninit;
use hyper_os::{
    Error, Result, Status,
    capability_channel::{CapabilityChannel, CapabilityDisposition, CapabilityReceiveSlot},
    handle::{ByteChannelObject, CapabilityChannelObject, OwnedHandle, Rights, RightsOffer},
    startup::{Startup, StartupPurpose},
};
use std::{process::ExitCode, time::Duration};

const MIGRATION: StartupPurpose<CapabilityChannelObject> = StartupPurpose::new(0x8008_0010);
const READY: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x8008_0020);
const BOOT_READY: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x8006_0001);
const INVENTORY: &[u8] = b"PROFILE-OPAQUE-OWNER/1";
const SENTINEL: &[u8] = b"storage ownership survived bootstrap retirement";
const ACK: &[u8] = b"PROFILE-OWNER-RECEIVED/1";

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            // Formal stdio reaches journal; bootstrap errors remain visible
            // through its supervisor even without a provider log capability.
            eprintln!("PROFILE_STORAGE_FAILED: {error:?}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<()> {
    let mut startup = hyper_rt::process::startup()?;
    let ready = startup.take_optional(READY)?;
    let migration = CapabilityChannel::from_handle(startup.take(MIGRATION)?);
    let until = hyper_os::time::deadline_after(Duration::from_secs(30))?.as_raw();
    if let Some(ready) = ready {
        successor(&startup, &migration, ready, until)
    } else {
        predecessor(&mut startup, &migration, until)
    }
}

fn predecessor(startup: &mut Startup, migration: &CapabilityChannel, until: u64) -> Result<()> {
    let (writer, reader) = hyper_os::channel::create_pair()?;
    writer.as_byte_channel().send(SENTINEL)?;
    drop(writer);
    startup
        .take(BOOT_READY)?
        .as_byte_channel()
        .send(b"HYPER-IO-READY/1\n")?;
    let mut reader = Some(reader);
    loop {
        let result = migration.try_send(
            INVENTORY,
            &mut [CapabilityDisposition::move_handle(
                &mut reader,
                RightsOffer::Exact(Rights::READ.union(Rights::WAIT)),
            )?],
        );
        match result {
            Ok(()) => break,
            Err(Error::Status(Status::WOULD_BLOCK)) => retry(until)?,
            Err(error) => return Err(error),
        }
    }
    if reader.is_some() {
        return Err(Error::InvalidResponse);
    }
    let mut bytes = [MaybeUninit::uninit(); 64];
    let reply = migration.receive(until, &mut bytes, &mut [])?;
    if reply.bytes() != ACK || reply.capability_count() != 0 {
        return Err(Error::InvalidResponse);
    }
    // The formal coordinator, not the provider protocol, owns predecessor stop.
    idle()
}

fn successor(
    startup: &Startup,
    migration: &CapabilityChannel,
    ready: OwnedHandle<ByteChannelObject>,
    until: u64,
) -> Result<()> {
    hyper_os::memory::lock_current_memory(startup.borrow(hyper_os::startup::ROOT_VMAR)?)?;
    let mut bytes = [MaybeUninit::uninit(); 64];
    let mut slots = [CapabilityReceiveSlot::new::<ByteChannelObject>(
        Rights::READ.union(Rights::WAIT),
    )];
    let message = migration.receive(until, &mut bytes, &mut slots)?;
    if message.bytes() != INVENTORY || message.capability_count() != 1 {
        return Err(Error::InvalidResponse);
    }
    let owner = slots[0]
        .take::<ByteChannelObject>()?
        .ok_or(Error::MissingHandle)?;
    let mut sentinel = [0; 64];
    let size = owner.as_byte_channel().try_receive(&mut sentinel)?;
    if &sentinel[..size] != SENTINEL {
        return Err(Error::InvalidResponse);
    }
    loop {
        match migration.try_send(ACK, &mut []) {
            Ok(()) => break,
            Err(Error::Status(Status::WOULD_BLOCK)) => retry(until)?,
            Err(error) => return Err(error),
        }
    }
    println!("PROFILE_STORAGE_OWNER_ADOPTED");
    ready.as_byte_channel().send(b"HYPER-SERVICE-ADOPTED/1")?;
    // Keep the transferred authority alive while system supervision owns us.
    loop {
        std::hint::black_box(&owner);
        std::thread::sleep(Duration::from_secs(60));
    }
}

fn retry(until: u64) -> Result<()> {
    if hyper_os::time::monotonic_now()?.as_nanoseconds() >= until {
        return Err(Error::Status(Status::TIMED_OUT));
    }
    std::thread::sleep(Duration::from_millis(1));
    Ok(())
}

fn idle() -> Result<()> {
    loop {
        std::thread::sleep(Duration::from_secs(60));
    }
}
