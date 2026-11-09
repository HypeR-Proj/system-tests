// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Native capability lifetime, concurrent requests and command inheritance.

use hyper_service::random::{self, Client};
use std::{sync::Arc, time::Duration};
type Result<T = ()> = std::result::Result<T, Box<dyn std::error::Error>>;

unsafe extern "C" {
    fn hyper_random_fill(buffer: *mut core::ffi::c_void, length: usize, deadline: u64) -> i64;
}
fn retained_fill() -> Result {
    let mut bytes = [0u8; 257];
    let until = hyper_os::time::deadline_after(Duration::from_secs(30))?.as_raw();
    // SAFETY: buffer remains exclusively writable for its complete length.
    let status = unsafe { hyper_random_fill(bytes.as_mut_ptr().cast(), bytes.len(), until) };
    if status != 0 {
        return Err(hyper_os::Error::Status(hyper_os::Status::from_raw(status)).into());
    }
    // Success verifies the transport/full-fill contract, not statistical quality.
    bytes.fill(0);
    Ok(())
}

pub fn child() -> Result {
    let mut startup = hyper_rt::process::startup()?;
    drop(startup.take(random::CLIENT)?);
    drop(startup);
    retained_fill()?;
    println!("RANDOM_CHILD_RETAINED_OK");
    Ok(())
}
pub fn denied() -> Result {
    let mut startup = hyper_rt::process::startup()?;
    if startup.take_optional(random::CLIENT)?.is_some() {
        return Err("unexpected random grant".into());
    }
    let mut bytes = [0u8; 1];
    // SAFETY: one exclusively writable byte; absence must fail without transport.
    let status = unsafe { hyper_random_fill(bytes.as_mut_ptr().cast(), bytes.len(), u64::MAX) };
    if hyper_os::Status::from_raw(status) != hyper_os::Status::ACCESS_DENIED {
        return Err("missing random capability did not fail explicitly".into());
    }
    println!("RANDOM_DENIED_OK");
    Ok(())
}
pub fn verify() -> Result {
    let mut startup = hyper_rt::process::startup()?;
    let connector = startup.take(random::CLIENT)?;
    let raw = hyper_os::capability_channel::CapabilityChannel::from_handle(
        connector.as_handle_ref().duplicate(random::CLIENT_RIGHTS)?,
    );
    let client = Arc::new(Client::from_handle(connector));
    drop(startup);
    let until = hyper_os::time::deadline_after(Duration::from_secs(30))?.as_raw();
    for malformed in [false, true] {
        let blocked = full_reply(&raw, malformed, until)?;
        let mut probe = [0u8; 1];
        client.fill(&mut probe, until)?;
        drop(blocked);
    }
    let mut workers = Vec::new();
    for _ in 0..4 {
        let client = client.clone();
        workers.push(std::thread::spawn(move || {
            let mut bytes = [0u8; 513];
            let result = client.fill(&mut bytes, until);
            bytes.fill(0);
            result
        }));
    }
    // Join every admitted worker even if one fails.
    let mut failed = false;
    for worker in workers {
        failed |= !matches!(worker.join(), Ok(Ok(())));
    }
    if failed {
        return Err("concurrent initialized random requests failed".into());
    }
    drop(client);
    retained_fill()?;
    let status = std::process::Command::new("/bin/service-probe")
        .arg("random-child")
        .status()?;
    if !status.success() {
        return Err("Command did not inherit random client".into());
    }
    println!("RANDOM_NATIVE_OK");
    Ok(())
}

fn full_reply(
    connector: &hyper_os::capability_channel::CapabilityChannel,
    malformed: bool,
    until: u64,
) -> Result<hyper_os::handle::OwnedHandle<hyper_os::handle::ByteChannelObject>> {
    use hyper_os::{capability_channel::CapabilityDisposition, handle::RightsOffer};
    let (reader, writer) = hyper_os::channel::create_pair()?;
    let mut full = false;
    for _ in 0..128 {
        match writer.as_byte_channel().try_send(&[0]) {
            Ok(()) => (),
            Err(hyper_os::Error::Status(hyper_os::Status::WOULD_BLOCK)) => {
                full = true;
                break;
            }
            Err(error) => return Err(error.into()),
        }
    }
    if !full {
        return Err("reply fixture did not reach bounded capacity".into());
    }
    let mut request = random::Request::new(1, until)?.encode();
    if malformed {
        request[0] ^= 1;
    }
    let mut writer = Some(writer);
    hyper_service_ipc::send(
        connector,
        until,
        &request,
        &mut [CapabilityDisposition::move_handle(
            &mut writer,
            RightsOffer::Exact(random::REPLY_RIGHTS),
        )?],
    )?;
    // Keep the unread peer alive while a second independent client completes.
    Ok(reader)
}
