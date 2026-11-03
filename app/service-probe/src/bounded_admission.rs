// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Exercise service-owned admission implementations on real Native channels.

use hyper_os::capability_channel::{CapabilityChannel, CapabilityDisposition};
use hyper_os::handle::{
    ByteChannelObject, CapabilityChannelObject, OwnedHandle, Rights, RightsOffer,
};
use hyper_os::wait::{ObjectSignals, WaitItem, wait_many};
use hyper_os::{Error, Result, Status};
use std::time::Duration;

fn deadline(seconds: u64) -> Result<u64> {
    Ok(hyper_os::time::deadline_after(Duration::from_secs(seconds))?.as_raw())
}

fn read(channel: &OwnedHandle<ByteChannelObject>, until: u64) -> Result<Vec<u8>> {
    let mut bytes = vec![0; 128];
    loop {
        match channel.as_byte_channel().try_receive(&mut bytes) {
            Ok(length) => {
                bytes.truncate(length);
                return Ok(bytes);
            }
            Err(Error::Status(Status::WOULD_BLOCK)) => {}
            Err(error) => return Err(error),
        }
        wait_many(
            &[WaitItem::new(
                channel.as_handle_ref(),
                ObjectSignals::<ByteChannelObject>::READABLE
                    .union(ObjectSignals::<ByteChannelObject>::PEER_CLOSED),
            )],
            until,
        )?;
    }
}

fn attach_fs(public: &CapabilityChannel) -> Result<OwnedHandle<ByteChannelObject>> {
    let (client, server) = hyper_os::channel::create_pair()?;
    let mut server = Some(server);
    hyper_service_ipc::send(
        public,
        deadline(2)?,
        hyper_service::filesystem::CONTROL_CONNECT,
        &mut [CapabilityDisposition::move_handle(
            &mut server,
            RightsOffer::Exact(Rights::READ.union(Rights::WRITE).union(Rights::WAIT)),
        )?],
    )?;
    Ok(client)
}

fn filesystem() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let (endpoint, client) = CapabilityChannel::create()?;
    let listener = hyper_fs_backend::admission::Listener::start(endpoint)?;
    let worker = std::thread::Builder::new()
        .name("fs-admission-probe".into())
        .spawn(move || -> Result<()> {
            for _ in 0..2 {
                let request = listener.receive()?;
                if request.bytes != b"healthy" {
                    return Err(Error::InvalidResponse);
                }
                request.connection.as_byte_channel().try_send(b"served")?;
            }
            Ok(())
        })?;
    let checked = (|| -> Result<()> {
        let mut silent = vec![attach_fs(&client)?];
        let healthy = attach_fs(&client)?;
        healthy.as_byte_channel().try_send(b"healthy")?;
        if read(&healthy, deadline(2)?)? != b"served" {
            return Err(Error::InvalidResponse);
        }
        drop(healthy);
        // The completed worker is reaped before testing the exact capacity.
        std::thread::sleep(Duration::from_millis(300));
        for _ in 1..8 {
            silent.push(attach_fs(&client)?);
        }
        let excess = attach_fs(&client)?;
        if read(&excess, deadline(2)?)? != b"ERROR\nmount admission capacity reached" {
            return Err(Error::InvalidResponse);
        }
        let expiry = deadline(7)?;
        for channel in &silent {
            if read(channel, expiry)? != b"ERROR\nmount request timed out" {
                return Err(Error::InvalidResponse);
            }
        }
        drop(silent);
        // Expiry must release worker slots, not merely write an error message.
        std::thread::sleep(Duration::from_millis(300));
        let healthy = attach_fs(&client)?;
        healthy.as_byte_channel().try_send(b"healthy")?;
        if read(&healthy, deadline(2)?)? != b"served" {
            return Err(Error::InvalidResponse);
        }
        Ok(())
    })();
    // Public closure releases a blocked receiver even when a probe step failed.
    drop(client);
    let joined = worker
        .join()
        .map_err(|_| "filesystem admission worker terminated")?;
    checked?;
    joined?;
    Ok(())
}

fn attach_io(public: &CapabilityChannel, message: &[u8]) -> Result<CapabilityChannel> {
    let (client, server) = CapabilityChannel::create()?;
    let mut server = Some(server.into_handle());
    hyper_service_ipc::send(
        public,
        deadline(2)?,
        message,
        &mut [CapabilityDisposition::move_handle(
            &mut server,
            RightsOffer::Exact(hyper_service::io::SESSION_RIGHTS),
        )?],
    )?;
    Ok(client)
}

fn wait_closed(channel: &CapabilityChannel, until: u64) -> Result<()> {
    wait_many(
        &[WaitItem::new(
            channel.as_handle_ref(),
            ObjectSignals::<CapabilityChannelObject>::PEER_CLOSED,
        )],
        until,
    )?;
    Ok(())
}

fn io() -> Result<()> {
    use hyper_io_runtime_system::admission::{Listener, Request};
    let (endpoint, client) = CapabilityChannel::create()?;
    let listener = Listener::start(endpoint)?;
    let connection =
        hyper_service::io::encode_connect(1, "system").ok_or(Error::InvalidResponse)?;
    let mut silent = Vec::new();
    for _ in 0..8 {
        silent.push(attach_io(&client, &connection)?);
    }
    let excess = attach_io(&client, &connection)?;
    wait_closed(&excess, deadline(2)?)?;
    let observer = attach_io(&client, hyper_service::io::OBSERVE_MESSAGE)?;
    wait_many(
        &[WaitItem::new(
            listener.bell.as_handle_ref(),
            ObjectSignals::<ByteChannelObject>::READABLE,
        )],
        deadline(2)?,
    )?;
    let Some(Request::Observe(accepted)) = listener.receive()? else {
        return Err(Error::InvalidResponse);
    };
    drop(accepted);
    wait_closed(&observer, deadline(2)?)?;
    // Cancel a private handshake, then observe its completion before reusing
    // the slot. No VM authority is needed to test this admission boundary.
    drop(silent.pop());
    wait_many(
        &[WaitItem::new(
            listener.bell.as_handle_ref(),
            ObjectSignals::<ByteChannelObject>::READABLE,
        )],
        deadline(2)?,
    )?;
    if listener.receive()?.is_some() {
        return Err(Error::InvalidResponse);
    }
    std::thread::sleep(Duration::from_millis(300));
    let recovered = attach_io(&client, &connection)?;
    match wait_closed(
        &recovered,
        hyper_os::time::deadline_after(Duration::from_millis(100))?.as_raw(),
    ) {
        Err(Error::Status(Status::TIMED_OUT)) => {}
        _ => return Err(Error::InvalidResponse),
    }
    // Dropping the owner cancels all incomplete 60-second handshakes promptly.
    drop(listener);
    for channel in silent.iter().chain([&recovered]) {
        wait_closed(channel, deadline(2)?)?;
    }
    Ok(())
}

pub fn verify() -> std::result::Result<(), Box<dyn std::error::Error>> {
    filesystem()?;
    io()?;
    println!("SERVICE_BOUNDED_ADMISSION_OK");
    Ok(())
}

pub fn journal() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let startup = hyper_rt::process::startup()?;
    let connect = || {
        hyper_component::connection::connect(
            startup.borrow(hyper_component::CONNECTOR)?,
            "journal",
            deadline(2)?,
        )
    };
    let query = hyper_journal::Query {
        component: None,
        after: 0,
        through: None,
        limit: 1,
    };
    let mut clients = Vec::new();
    // Retained idle endpoints must not exhaust an eight-session worker cap.
    for _ in 0..16 {
        let client = hyper_journal::open_query(connect()?, deadline(2)?)?;
        let _: hyper_journal::Page = hyper_service_ipc::request(client.as_handle_ref(), &query)?;
        clients.push(client);
    }
    let mut silent = Vec::new();
    std::thread::sleep(Duration::from_millis(300));
    for _ in 0..8 {
        silent.push(connect()?);
    }
    let excess = connect()?;
    wait_closed(&excess, deadline(2)?)?;
    // Existing endpoints remain live while all delegation slots are occupied.
    let _: hyper_journal::Page = hyper_service_ipc::request(clients[0].as_handle_ref(), &query)?;
    let expiry = deadline(7)?;
    for session in silent {
        wait_closed(&session, expiry)?;
    }
    std::thread::sleep(Duration::from_millis(300));
    let recovered = hyper_journal::open_query(connect()?, deadline(2)?)?;
    let _: hyper_journal::Page = hyper_service_ipc::request(recovered.as_handle_ref(), &query)?;
    let _: hyper_journal::Page = hyper_service_ipc::request(clients[0].as_handle_ref(), &query)?;
    println!("JOURNAL_ADMISSION_RECOVERY_OK");
    Ok(())
}
