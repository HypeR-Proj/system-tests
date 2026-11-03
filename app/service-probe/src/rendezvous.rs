// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Exercise receive-origin and MOVE ownership through the installed Native SDK.

use hyper_os::capability_channel::{
    CapabilityChannel, CapabilityDisposition, CapabilityReceiveSlot,
};
use hyper_os::handle::{ByteChannelObject, CapabilityChannelObject, Rights, RightsOffer};
use hyper_os::{Error, ReceiveRejection, Status};
use std::mem::MaybeUninit;

pub fn verify() -> Result<(), Box<dyn std::error::Error>> {
    let (sender, receiver) = CapabilityChannel::create()?;
    // A receiver invocation lacking READ must stay distinguishable from a
    // matched sender whose offered rights do not satisfy the receive contract.
    let denied = CapabilityChannel::from_handle(receiver.as_handle_ref().duplicate(Rights::WRITE)?);
    let mut bytes = [MaybeUninit::uninit(); 1];
    if denied
        .receive(hyper_service_ipc::deadline()?, &mut bytes, &mut [])
        .err()
        != Some(Error::Status(Status::ACCESS_DENIED))
    {
        return Err("receiver-local rights failure lost its origin".into());
    }
    drop(denied);
    let (source, observer) = hyper_os::channel::create_pair()?;
    let mut source = Some(source);
    let worker = std::thread::Builder::new()
        .name("receive-origin".into())
        .spawn(move || -> hyper_os::Result<()> {
            for attempt in 0..4 {
                let mut bytes = [MaybeUninit::uninit(); 1];
                let mut slots = [if attempt == 1 {
                    CapabilityReceiveSlot::new::<CapabilityChannelObject>(Rights::WRITE)
                } else {
                    CapabilityReceiveSlot::new::<ByteChannelObject>(Rights::WRITE)
                }];
                let capacity = usize::from(attempt != 0);
                let result = receiver.receive(
                    hyper_service_ipc::deadline()?,
                    &mut bytes[..capacity],
                    &mut slots,
                );
                let expected = match attempt {
                    0 => Some(Error::ReceiveRejected(ReceiveRejection::Capacity {
                        bytes: 1,
                        handles: 1,
                    })),
                    1 => Some(Error::ReceiveRejected(ReceiveRejection::Status(
                        Status::BAD_HANDLE,
                    ))),
                    2 => Some(Error::ReceiveRejected(ReceiveRejection::Status(
                        Status::ACCESS_DENIED,
                    ))),
                    _ => None,
                };
                if let Some(expected) = expected {
                    if result.err() != Some(expected) || slots[0].is_occupied() {
                        return Err(Error::InvalidResponse);
                    }
                } else {
                    let message = result?;
                    if message.bytes() != b"x" || message.capability_count() != 1 {
                        return Err(Error::InvalidResponse);
                    }
                    let received = slots[0]
                        .take::<ByteChannelObject>()?
                        .ok_or(Error::MissingHandle)?;
                    received
                        .as_byte_channel()
                        .try_send(b"ownership committed")?;
                }
            }
            Ok(())
        })?;
    let sent = (|| -> hyper_os::Result<()> {
        for attempt in 0..4 {
            let offer = if attempt == 2 {
                Rights::READ
            } else {
                Rights::WRITE
            };
            let result = hyper_service_ipc::send(
                &sender,
                hyper_service_ipc::deadline()?,
                b"x",
                &mut [CapabilityDisposition::move_handle(
                    &mut source,
                    RightsOffer::Exact(offer),
                )?],
            );
            let expected = match attempt {
                0 => Some(Error::Status(Status::BUFFER_TOO_SMALL)),
                1 => Some(Error::Status(Status::BAD_HANDLE)),
                2 => Some(Error::Status(Status::ACCESS_DENIED)),
                _ => None,
            };
            if result.err() != expected || source.is_some() != expected.is_some() {
                return Err(Error::InvalidResponse);
            }
            if let Some(source) = source.as_ref() {
                source.info()?;
            }
        }
        Ok(())
    })();
    // Close our endpoint before joining on any failure so the peer cannot stay
    // blocked until an unrelated fixture timeout. The worker also has a deadline.
    drop(sender);
    let received = worker.join().map_err(|_| "rendezvous worker terminated")?;
    sent?;
    received?;
    let mut proof = [0; 32];
    let length = observer.as_byte_channel().try_receive(&mut proof)?;
    if &proof[..length] != b"ownership committed" {
        return Err("committed capability did not retain its intended object".into());
    }
    println!("SERVICE_RECEIVE_ORIGIN_OK");
    Ok(())
}
