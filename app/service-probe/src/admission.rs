// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Bad requests must not withdraw a shared service endpoint.

use hyper_os::capability_channel::{CapabilityChannel, CapabilityDisposition};
use hyper_os::handle::{Rights, RightsOffer};
use hyper_os::{Error, Result, Status};
use hyper_service_ipc::{Admission, Rejection};

pub fn verify() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let (listener, client) = CapabilityChannel::create()?;
    let denied = CapabilityChannel::from_handle(listener.as_handle_ref().duplicate(Rights::WRITE)?);
    if hyper_service_ipc::receive::<String>(&denied, hyper_service_ipc::deadline()?).err()
        != Some(Error::Status(Status::ACCESS_DENIED))
    {
        return Err("local listener failure was discarded as client input".into());
    }
    drop(denied);
    let worker = std::thread::Builder::new()
        .name("admission-recovery".into())
        .spawn(move || -> Result<()> {
            for attempt in 0..6 {
                let admission = hyper_service_ipc::receive::<String>(
                    &listener,
                    hyper_service_ipc::deadline()?,
                )?;
                let expected = if attempt < 2 {
                    Rejection::InvalidMessage
                } else {
                    Rejection::Transfer(match attempt {
                        2 => hyper_os::ReceiveRejection::Capacity {
                            bytes: (hyper_service_ipc::MAX_REQUEST + 1) as u64,
                            handles: 1,
                        },
                        3 => hyper_os::ReceiveRejection::Status(Status::BAD_HANDLE),
                        4 => hyper_os::ReceiveRejection::Status(Status::ACCESS_DENIED),
                        _ => hyper_os::ReceiveRejection::Capacity {
                            bytes: 4,
                            handles: 2,
                        },
                    })
                };
                if !matches!(admission, Admission::Rejected(reason) if reason == expected) {
                    return Err(Error::InvalidResponse);
                }
                // A different private reply channel succeeds after each refusal.
                let Admission::Accepted(call) = hyper_service_ipc::receive::<String>(
                    &listener,
                    hyper_service_ipc::deadline()?,
                )?
                else {
                    return Err(Error::InvalidResponse);
                };
                if call.request != format!("valid-{attempt}") {
                    return Err(Error::InvalidResponse);
                }
                let reply = call.request.clone();
                call.respond(&reply)?;
            }
            Ok(())
        })?;
    let sent = (|| -> Result<()> {
        for attempt in 0..6 {
            let (observer, source) = hyper_os::channel::create_pair()?;
            let mut source = Some(source);
            let (wrong_kind, _) = CapabilityChannel::create()?;
            let mut wrong_kind = Some(wrong_kind.into_handle());
            let oversized = vec![b'x'; hyper_service_ipc::MAX_REQUEST + 1];
            let mut extra = None;
            let result = match attempt {
                5 => {
                    let (owner, peer) = hyper_os::channel::create_pair()?;
                    drop(peer);
                    extra = Some(owner);
                    let rights = RightsOffer::Exact(Rights::WRITE.union(Rights::WAIT));
                    hyper_service_ipc::send(
                        &client,
                        hyper_service_ipc::deadline()?,
                        b"more",
                        &mut [
                            CapabilityDisposition::move_handle(&mut source, rights)?,
                            CapabilityDisposition::move_handle(&mut extra, rights)?,
                        ],
                    )
                }
                1 => hyper_service_ipc::send(
                    &client,
                    hyper_service_ipc::deadline()?,
                    b"\"missing reply\"",
                    &mut [],
                ),
                3 => hyper_service_ipc::send(
                    &client,
                    hyper_service_ipc::deadline()?,
                    b"\"wrong kind\"",
                    &mut [CapabilityDisposition::move_handle(
                        &mut wrong_kind,
                        RightsOffer::Exact(Rights::WRITE.union(Rights::WAIT)),
                    )?],
                ),
                _ => hyper_service_ipc::send(
                    &client,
                    hyper_service_ipc::deadline()?,
                    if attempt == 2 {
                        &oversized
                    } else {
                        b"malformed JSON"
                    },
                    &mut [CapabilityDisposition::move_handle(
                        &mut source,
                        RightsOffer::Exact(if attempt == 4 {
                            Rights::READ
                        } else {
                            Rights::WRITE.union(Rights::WAIT)
                        }),
                    )?],
                ),
            };
            let expected = match attempt {
                2 | 5 => Some(Error::Status(Status::BUFFER_TOO_SMALL)),
                3 => Some(Error::Status(Status::BAD_HANDLE)),
                4 => Some(Error::Status(Status::ACCESS_DENIED)),
                _ => None,
            };
            if result.err() != expected {
                return Err(Error::InvalidResponse);
            }
            if attempt == 0 {
                // Successfully transferred but malformed protocol: the server
                // drops the reply owner without publishing application state.
                hyper_os::wait::wait_many(
                    &[hyper_os::wait::WaitItem::new(
                        observer.as_handle_ref(),
                        hyper_os::wait::ObjectSignals::<hyper_os::handle::ByteChannelObject>::PEER_CLOSED,
                    )],
                    hyper_service_ipc::deadline()?,
                )?;
            } else if attempt == 2 || attempt == 4 || attempt == 5 {
                source.as_ref().ok_or(Error::MissingHandle)?.info()?;
                if attempt == 5 {
                    extra.as_ref().ok_or(Error::MissingHandle)?.info()?;
                }
            } else if attempt == 3 {
                wrong_kind.as_ref().ok_or(Error::MissingHandle)?.info()?;
            }
            let message = format!("valid-{attempt}");
            let reply: String = hyper_service_ipc::request(client.as_handle_ref(), &message)?;
            if reply != message {
                return Err(Error::InvalidResponse);
            }
        }
        Ok(())
    })();
    drop(client);
    let received = worker.join().map_err(|_| "admission worker terminated")?;
    sent?;
    received?;
    verify_stream()?;
    println!("SERVICE_ADMISSION_RECOVERY_OK");
    Ok(())
}

fn verify_stream() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let (listener, client) = CapabilityChannel::create()?;
    let (observer, channel) = hyper_os::channel::create_pair()?;
    let worker = std::thread::Builder::new()
        .name("stream-refusal".into())
        .spawn(move || -> Result<()> {
            if hyper_os::stream::accept_until(&listener, hyper_service_ipc::deadline()?).err()
                != Some(Error::InvalidStreamRequest)
            {
                return Err(Error::InvalidResponse);
            }
            let accepted =
                hyper_os::stream::accept_until(&listener, hyper_service_ipc::deadline()?)?;
            if accepted.access != hyper_os::stream::ACCESS_WRITE {
                return Err(Error::InvalidResponse);
            }
            accepted.channel.as_byte_channel().try_send(b"accepted")
        })?;
    let sent = (|| -> Result<()> {
        hyper_service_ipc::send(&client, hyper_service_ipc::deadline()?, b"invalid", &mut [])?;
        let mut channel = Some(channel);
        let mut message = [0; 16];
        message[..8].copy_from_slice(&hyper_os::stream::OPEN_MAGIC);
        message[8..].copy_from_slice(&hyper_os::stream::ACCESS_WRITE.to_le_bytes());
        hyper_service_ipc::send(
            &client,
            hyper_service_ipc::deadline()?,
            &message,
            &mut [CapabilityDisposition::move_handle(
                &mut channel,
                RightsOffer::Exact(hyper_os::stream::SESSION_RIGHTS),
            )?],
        )
    })();
    drop(client);
    let received = worker
        .join()
        .map_err(|_| "stream admission worker terminated")?;
    sent?;
    received?;
    let mut bytes = [0; 8];
    if observer.as_byte_channel().try_receive(&mut bytes)? != bytes.len() || &bytes != b"accepted" {
        return Err("valid stream was lost after malformed admission".into());
    }
    Ok(())
}

/// Inject malformed JSON into a real service endpoint before its next valid call.
pub fn malformed(
    connector: hyper_os::handle::HandleRef<'_, hyper_os::handle::CapabilityChannelObject>,
) -> Result<()> {
    let sender =
        CapabilityChannel::from_handle(connector.duplicate(hyper_service_ipc::CLIENT_RIGHTS)?);
    let (observer, reply) = hyper_os::channel::create_pair()?;
    let mut reply = Some(reply);
    hyper_service_ipc::send(
        &sender,
        hyper_service_ipc::deadline()?,
        b"not JSON",
        &mut [CapabilityDisposition::move_handle(
            &mut reply,
            RightsOffer::Exact(Rights::WRITE.union(Rights::WAIT)),
        )?],
    )?;
    hyper_os::wait::wait_many(
        &[hyper_os::wait::WaitItem::new(
            observer.as_handle_ref(),
            hyper_os::wait::ObjectSignals::<hyper_os::handle::ByteChannelObject>::PEER_CLOSED,
        )],
        hyper_service_ipc::deadline()?,
    )?;
    Ok(())
}
