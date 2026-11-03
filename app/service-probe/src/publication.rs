// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Real request owners around the production lifecycle acceptance boundary.

use hyper_component::{Request, State};
use hyper_component_manager::{command::apply_pending, lifecycle::Lifecycle};
use hyper_os::capability_channel::{CapabilityChannel, CapabilityDisposition};
use hyper_os::handle::{ByteChannelObject, OwnedHandle, Rights, RightsOffer};
use hyper_os::{Error, Result, Status};
use hyper_service_ipc::{Admission, Call};
use std::time::Duration;

type ProbeResult<T = ()> = std::result::Result<T, Box<dyn std::error::Error>>;

fn deadline() -> Result<u64> {
    Ok(hyper_os::time::deadline_after(Duration::from_secs(5))?.as_raw())
}

fn pending(
    operation: &str,
    until: u64,
) -> ProbeResult<(Call<Request>, OwnedHandle<ByteChannelObject>)> {
    let (listener, client) = CapabilityChannel::create()?;
    let (reader, reply) = hyper_os::channel::create_pair()?;
    let worker = std::thread::Builder::new()
        .name("pending-command-probe".into())
        .spawn(move || -> Result<Call<Request>> {
            match hyper_service_ipc::receive(&listener, deadline()?)? {
                Admission::Accepted(call) => Ok(call),
                Admission::Rejected(_) => Err(Error::InvalidResponse),
            }
        })?;
    let mut reply = Some(reply);
    let bytes = format!(
        r#"{{"deadline_ns":{until},"request":{{"operation":"{operation}","path":"/server"}}}}"#
    );
    let sent = hyper_service_ipc::send(
        &client,
        deadline()?,
        bytes.as_bytes(),
        &mut [CapabilityDisposition::move_handle(
            &mut reply,
            RightsOffer::Exact(Rights::WRITE.union(Rights::WAIT)),
        )?],
    );
    drop(client);
    let received = worker
        .join()
        .map_err(|_| "pending command receiver terminated")?;
    sent?;
    Ok((received?, reader))
}

fn lifecycle() -> ProbeResult<Lifecycle> {
    let graph = hyper_component_config::Topology::parse(
        "root",
        [
            (
                "root.json".into(),
                br#"{"name":"root","children":[{"name":"server","component":"server"}]}"#.to_vec(),
            ),
            (
                "server.json".into(),
                br#"{"name":"server","program":{"image":"/svc/server","startup":"lazy"}}"#.to_vec(),
            ),
        ],
    )?;
    Ok(Lifecycle::new(graph))
}

fn commands() -> ProbeResult {
    let mut state = lifecycle()?;
    let (call, peer) = pending("start", deadline()?)?;
    if !call.is_pending()? {
        return Err("live queued command was canceled".into());
    }
    drop(peer);
    if apply_pending(&mut state, call)?.is_some()
        || state.snapshot(0).revision != 0
        || !state.begin(0).is_empty()
    {
        return Err("closed queued command changed lifecycle intent".into());
    }
    // The transport accepted this call while live, but the state owner is late.
    let until = deadline()?;
    let (call, peer) = pending("start", until)?;
    hyper_os::thread::sleep_until(until)?;
    if apply_pending(&mut state, call)?.is_some()
        || state.snapshot(0).revision != 0
        || !state.begin(0).is_empty()
    {
        return Err("expired queued command changed lifecycle intent".into());
    }
    drop(peer);
    let (call, peer) = pending("start", deadline()?)?;
    let applied = apply_pending(&mut state, call)?.ok_or("healthy command was not accepted")?;
    applied.receipt.result.as_ref().map_err(|e| e.clone())?;
    let revision = applied.receipt.snapshot.revision;
    drop(peer);
    if applied.call.respond(&"lost").is_ok()
        || revision == 0
        || state.snapshot(0).revision != revision
        || state.begin(0) != [(1, 1)]
    {
        return Err("lost reply rolled back accepted start or duplicated generation".into());
    }
    state.started(1, 1, 42)?;
    let (call, peer) = pending("stop", deadline()?)?;
    let applied = apply_pending(&mut state, call)?.ok_or("healthy stop was not accepted")?;
    applied.receipt.result.as_ref().map_err(|e| e.clone())?;
    drop(peer);
    drop(applied);
    if state.status(1).state != State::Stopping || state.status(1).process != Some(42) {
        return Err("lost stop reply released generation before retirement".into());
    }
    state.retired(1, 1, true, 1)?;
    if state.status(1).state != State::Stopped || !state.begin(2).is_empty() {
        return Err("accepted stop did not survive reply loss".into());
    }
    Ok(())
}

fn filesystem() -> ProbeResult {
    use hyper_fs_backend::admission::{Listener, Publication};
    let (endpoint, client) = CapabilityChannel::create()?;
    let listener = Listener::start(endpoint)?;
    for canceled in [true, false] {
        let (peer, server) = hyper_os::channel::create_pair()?;
        let mut server = Some(server);
        hyper_service_ipc::send(
            &client,
            deadline()?,
            hyper_service::filesystem::CONTROL_CONNECT,
            &mut [CapabilityDisposition::move_handle(
                &mut server,
                RightsOffer::Exact(Rights::READ.union(Rights::WRITE).union(Rights::WAIT)),
            )?],
        )?;
        peer.as_byte_channel().try_send(b"queued-mount")?;
        let request = listener.receive()?;
        let publication = Publication::requested(request.connection.as_handle_ref());
        if canceled {
            drop(peer);
            if publication.check().err() != Some(Error::Status(Status::CANCELLED)) {
                return Err("closed queued filesystem command passed acceptance".into());
            }
        } else {
            publication.check()?;
            request.connection.as_byte_channel().try_send(b"accepted")?;
            let mut reply = [0; 16];
            if peer
                .as_byte_channel()
                .receive_until(&mut reply, deadline()?)?
                != 8
            {
                return Err("healthy filesystem caller did not follow cancellation".into());
            }
        }
    }
    drop(client);
    drop(listener);
    Ok(())
}

pub fn verify() -> ProbeResult {
    commands()?;
    filesystem()?;
    println!("SERVICE_PUBLICATION_OK");
    Ok(())
}
