// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Absolute budgets and reply backpressure through the production Native RPC.

use core::mem::MaybeUninit;
use hyper_os::capability_channel::{
    CapabilityChannel, CapabilityDisposition, CapabilityReceiveSlot,
};
use hyper_os::handle::{ByteChannelObject, OwnedHandle, Rights, RightsOffer};
use hyper_os::wait::{ObjectSignals, WaitItem, wait_many};
use hyper_os::{Error, Result, Status};
use hyper_service_ipc::{Admission, Rejection, RpcError, RpcResult};
use std::time::Duration;

fn deadline(milliseconds: u64) -> Result<u64> {
    Ok(hyper_os::time::deadline_after(Duration::from_millis(milliseconds))?.as_raw())
}

fn byte_deadlines() -> Result<()> {
    let (reader, writer) = hyper_os::channel::create_pair()?;
    if writer.as_byte_channel().send_until(b"late", 0).err()
        != Some(Error::Status(Status::TIMED_OUT))
    {
        return Err(Error::InvalidResponse);
    }
    let mut bytes = [0; 4];
    if reader.as_byte_channel().try_receive(&mut bytes).err()
        != Some(Error::Status(Status::WOULD_BLOCK))
    {
        return Err(Error::InvalidResponse);
    }
    writer.as_byte_channel().try_send(b"kept")?;
    if reader.as_byte_channel().receive_until(&mut bytes, 0).err()
        != Some(Error::Status(Status::TIMED_OUT))
    {
        return Err(Error::InvalidResponse);
    }
    if reader.as_byte_channel().try_receive(&mut bytes)? != 4 || &bytes != b"kept" {
        return Err(Error::InvalidResponse);
    }
    Ok(())
}

fn staged_delay() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let (listener, client) = CapabilityChannel::create()?;
    let until = deadline(2000)?;
    let worker = std::thread::Builder::new()
        .name("rpc-staged-delay".into())
        .spawn(move || -> RpcResult<()> {
            std::thread::sleep(Duration::from_millis(1200));
            let Admission::Accepted(call) =
                hyper_service_ipc::receive::<String>(&listener, deadline(5000)?)?
            else {
                return Err(RpcError::InvalidResponse);
            };
            if call.deadline() != until {
                return Err(RpcError::InvalidResponse);
            }
            std::thread::sleep(Duration::from_millis(1200));
            if call
                .respond_until(&"late", hyper_os::DEADLINE_INFINITE)
                .err()
                != Some(RpcError::Transport(Error::Status(Status::TIMED_OUT)))
            {
                return Err(RpcError::InvalidResponse);
            }
            Ok(())
        })?;
    let result =
        hyper_service_ipc::request_until::<_, String>(client.as_handle_ref(), &"staged", until);
    drop(client);
    let server = worker.join().map_err(|_| "staged RPC worker terminated")?;
    if result.err() != Some(RpcError::Transport(Error::Status(Status::TIMED_OUT))) {
        return Err("RPC renewed the caller budget between send and receive".into());
    }
    server?;
    Ok(())
}

fn raw_request(
    client: &CapabilityChannel,
    reply: OwnedHandle<ByteChannelObject>,
    until: u64,
    request_deadline: u64,
) -> Result<()> {
    let mut reply = Some(reply);
    let bytes = format!("{{\"deadline_ns\":{request_deadline},\"request\":\"probe\"}}");
    hyper_service_ipc::send(
        client,
        until,
        bytes.as_bytes(),
        &mut [CapabilityDisposition::move_handle(
            &mut reply,
            RightsOffer::Exact(Rights::WRITE.union(Rights::WAIT)),
        )?],
    )
}

fn slow_reply(partial: bool) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let (listener, client) = CapabilityChannel::create()?;
    let (unread, reply) = hyper_os::channel::create_pair()?;
    let mut full = false;
    for _ in 0..1024 {
        match reply.as_byte_channel().try_send(&[0; 256]) {
            Ok(()) => {}
            Err(Error::Status(Status::WOULD_BLOCK)) => {
                full = true;
                break;
            }
            Err(error) => return Err(error.into()),
        }
    }
    if !full {
        return Err("reply queue did not reach its bounded capacity".into());
    }
    if partial {
        // Leave exactly one slot for the header, then stall the payload.
        unread.as_byte_channel().try_receive(&mut [0; 256])?;
    }
    let worker = std::thread::Builder::new()
        .name("rpc-slow-reply".into())
        .spawn(move || -> RpcResult<()> {
            let Admission::Accepted(call) =
                hyper_service_ipc::receive::<String>(&listener, deadline(5000)?)?
            else {
                return Err(RpcError::InvalidResponse);
            };
            if call.respond_until(&"blocked", deadline(250)?).err()
                != Some(RpcError::Transport(Error::Status(Status::TIMED_OUT)))
            {
                return Err(RpcError::InvalidResponse);
            }
            let Admission::Accepted(call) =
                hyper_service_ipc::receive::<String>(&listener, deadline(5000)?)?
            else {
                return Err(RpcError::InvalidResponse);
            };
            call.respond(&"healthy")
        })?;
    let checked = (|| -> RpcResult<()> {
        let until = deadline(5000)?;
        raw_request(&client, reply, until, until)?;
        let response: String =
            hyper_service_ipc::request_until(client.as_handle_ref(), &"next", until)?;
        if response != "healthy" {
            return Err(RpcError::InvalidResponse);
        }
        wait_many(
            &[WaitItem::new(
                unread.as_handle_ref(),
                ObjectSignals::<ByteChannelObject>::PEER_CLOSED,
            )],
            until,
        )?;
        let mut header_seen = false;
        let mut bytes = [0; 256];
        loop {
            match unread.as_byte_channel().try_receive(&mut bytes) {
                Ok(n) if bytes[..n].starts_with(b"HYPER-REPLY/1") => header_seen = true,
                Ok(256) if bytes == [0; 256] => {}
                Err(Error::Status(Status::PEER_CLOSED)) => break,
                _ => return Err(RpcError::InvalidResponse),
            }
        }
        if header_seen != partial {
            return Err(RpcError::InvalidResponse);
        }
        Ok(())
    })();
    drop(client);
    drop(unread);
    let server = worker
        .join()
        .map_err(|_| "slow-reply RPC worker terminated")?;
    checked?;
    server?;
    Ok(())
}

fn expired_request() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let (listener, client) = CapabilityChannel::create()?;
    let (reader, reply) = hyper_os::channel::create_pair()?;
    let worker = std::thread::Builder::new()
        .name("rpc-expired-request".into())
        .spawn(move || -> RpcResult<()> {
            if !matches!(
                hyper_service_ipc::receive::<String>(&listener, deadline(5000)?)?,
                Admission::Rejected(Rejection::Expired)
            ) {
                return Err(RpcError::InvalidResponse);
            }
            let Admission::Accepted(call) =
                hyper_service_ipc::receive::<String>(&listener, deadline(5000)?)?
            else {
                return Err(RpcError::InvalidResponse);
            };
            call.respond(&"healthy")
        })?;
    let checked = (|| -> RpcResult<()> {
        let until = deadline(5000)?;
        raw_request(&client, reply, until, 0)?;
        wait_many(
            &[WaitItem::new(
                reader.as_handle_ref(),
                ObjectSignals::<ByteChannelObject>::PEER_CLOSED,
            )],
            until,
        )?;
        let response: String =
            hyper_service_ipc::request_until(client.as_handle_ref(), &"valid", until)?;
        if response != "healthy" {
            return Err(RpcError::InvalidResponse);
        }
        Ok(())
    })();
    drop(client);
    let server = worker
        .join()
        .map_err(|_| "expired-request RPC worker terminated")?;
    checked?;
    server?;
    Ok(())
}

fn invalid_response() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let (listener, client) = CapabilityChannel::create()?;
    let worker = std::thread::Builder::new()
        .name("rpc-invalid-response".into())
        .spawn(move || -> Result<()> {
            let mut bytes = [MaybeUninit::uninit(); hyper_service_ipc::MAX_REQUEST];
            let mut slots = [CapabilityReceiveSlot::new::<ByteChannelObject>(
                Rights::WRITE.union(Rights::WAIT),
            )];
            let message = listener.receive(deadline(5000)?, &mut bytes, &mut slots)?;
            if message.capability_count() != 1 {
                return Err(Error::InvalidResponse);
            }
            let reply = slots[0].take()?.ok_or(Error::MissingHandle)?;
            reply
                .as_byte_channel()
                .send_until(b"bad frame", deadline(1000)?)
        })?;
    let result = hyper_service_ipc::request::<_, String>(client.as_handle_ref(), &"request");
    drop(client);
    let server = worker
        .join()
        .map_err(|_| "invalid-response RPC worker terminated")?;
    if result.err() != Some(RpcError::InvalidResponse) {
        return Err("invalid peer reply was not classified as a protocol failure".into());
    }
    server?;
    Ok(())
}

fn inherited_reply_budget() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let (listener, client) = CapabilityChannel::create()?;
    let (reader, reply) = hyper_os::channel::create_pair()?;
    let mut filled = 0;
    loop {
        match reply.as_byte_channel().try_send(b"filler") {
            Ok(()) => filled += 1,
            Err(Error::Status(Status::WOULD_BLOCK)) => break,
            Err(error) => return Err(error.into()),
        }
    }
    let (started, ready) = std::sync::mpsc::sync_channel(1);
    let until = deadline(15000)?;
    let worker = std::thread::Builder::new()
        .name("rpc-long-reply".into())
        .spawn(move || -> RpcResult<()> {
            let Admission::Accepted(call) = hyper_service_ipc::receive::<String>(&listener, until)?
            else {
                return Err(RpcError::InvalidResponse);
            };
            started.send(()).map_err(|_| Error::InvalidResponse)?;
            call.respond(&"after five seconds")
        })?;
    let checked = (|| -> RpcResult<()> {
        raw_request(&client, reply, until, until)?;
        ready.recv().map_err(|_| Error::InvalidResponse)?;
        std::thread::sleep(Duration::from_millis(5500));
        let mut bytes = [0; 128];
        for _ in 0..filled {
            let n = reader.as_byte_channel().receive_until(&mut bytes, until)?;
            if &bytes[..n] != b"filler" {
                return Err(RpcError::InvalidResponse);
            }
        }
        let n = reader.as_byte_channel().receive_until(&mut bytes, until)?;
        if n != b"HYPER-REPLY/1".len() + 8 || !bytes[..n].starts_with(b"HYPER-REPLY/1") {
            return Err(RpcError::InvalidResponse);
        }
        let n = reader.as_byte_channel().receive_until(&mut bytes, until)?;
        if &bytes[..n] != b"\"after five seconds\"" {
            return Err(RpcError::InvalidResponse);
        }
        Ok(())
    })();
    drop(reader);
    drop(client);
    let server = worker.join().map_err(|_| "long-reply worker terminated")?;
    checked?;
    server?;
    Ok(())
}

fn recoverable_preparation() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let (listener, client) = CapabilityChannel::create()?;
    let worker = std::thread::Builder::new()
        .name("rpc-prepare-recovery".into())
        .spawn(move || -> RpcResult<()> {
            let Admission::Accepted(call) =
                hyper_service_ipc::receive::<String>(&listener, deadline(5000)?)?
            else {
                return Err(RpcError::InvalidResponse);
            };
            let oversized = "x".repeat(hyper_service_ipc::MAX_RESPONSE);
            if !matches!(
                hyper_service_ipc::PreparedResponse::encode(&oversized),
                Err(RpcError::Encoding {
                    reason: hyper_service_ipc::EncodingFailure::TooLarge { .. },
                    ..
                })
            ) {
                return Err(RpcError::InvalidResponse);
            }
            // No frame has been sent and the original call still owns its reply.
            let prepared = hyper_service_ipc::PreparedResponse::encode(&"application fallback")?;
            call.respond_prepared_until(prepared, deadline(1000)?)
        })?;
    let response = hyper_service_ipc::request::<_, String>(client.as_handle_ref(), &"prepare");
    drop(client);
    let server = worker
        .join()
        .map_err(|_| "reply preparation worker terminated")?;
    if response? != "application fallback" {
        return Err("lost reply after preparation failure".into());
    }
    server?;
    Ok(())
}

pub fn verify() -> std::result::Result<(), Box<dyn std::error::Error>> {
    byte_deadlines()?;
    staged_delay()?;
    slow_reply(false)?;
    slow_reply(true)?;
    inherited_reply_budget()?;
    recoverable_preparation()?;
    expired_request()?;
    invalid_response()?;
    println!("SERVICE_REQUEST_DEADLINES_OK");
    Ok(())
}
