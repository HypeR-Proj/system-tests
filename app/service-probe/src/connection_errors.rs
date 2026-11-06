// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Exercise real connector helpers through a consumer's boxed-error boundary.

use hyper_component::connection::{self, ConnectionError, Rejection, RejectionKind};
use hyper_os::capability_channel::CapabilityChannel;
use hyper_os::{Error, Status};
use hyper_service_ipc::Admission;

fn consume(client: &CapabilityChannel, operation: usize) -> Result<(), Box<dyn std::error::Error>> {
    match operation {
        0 => {
            connection::connect(
                client.as_handle_ref(),
                "probe",
                hyper_service_ipc::deadline()?,
            )?;
        }
        1 => connection::publish(client.as_handle_ref(), "probe")?,
        2 => connection::status(client.as_handle_ref(), "probe")?,
        3 => connection::report_failure(client.as_handle_ref(), "probe")?,
        _ => connection::report_critical_failure(client.as_handle_ref(), "probe")?,
    }
    Ok(())
}

pub fn verify() -> Result<(), Box<dyn std::error::Error>> {
    for kind in [
        RejectionKind::NotOffered,
        RejectionKind::InvalidRequest,
        RejectionKind::Unavailable,
        RejectionKind::Restarting,
        RejectionKind::TimedOut,
        RejectionKind::ResourceLimit,
    ] {
        for operation in 0..5 {
            let diagnostic = if operation == 3 {
                "\0🙂".repeat(256)
            } else {
                "provider unavailable: diagnostic retained".into()
            };
            let expected = diagnostic.clone();
            let (listener, client) = CapabilityChannel::create()?;
            let worker = std::thread::Builder::new()
                .name("connector-rejection".into())
                .spawn(move || -> hyper_os::Result<()> {
                    let Admission::Accepted(call) =
                        connection::receive(&listener, hyper_service_ipc::deadline()?)?
                    else {
                        return Err(Error::InvalidResponse);
                    };
                    connection::respond(
                        call.reply,
                        Some(Rejection {
                            kind,
                            message: diagnostic,
                        }),
                    );
                    Ok(())
                })?;
            let result = consume(&client, operation);
            worker
                .join()
                .map_err(|_| "connector responder terminated")??;
            let error = result
                .err()
                .ok_or("connector silently accepted rejection")?;
            if !matches!(error.downcast_ref::<ConnectionError>(),
                Some(ConnectionError::Rejected(rejection))
                    if rejection.kind == kind && !rejection.message.is_empty()
                        && expected.starts_with(&rejection.message)
                        && (operation == 3 || rejection.message == expected))
            {
                return Err(format!("connector lost rejection: {error}").into());
            }
        }
    }
    for operation in 0..5 {
        let (listener, client) = CapabilityChannel::create()?;
        drop(listener);
        let error = consume(&client, operation)
            .err()
            .ok_or("closed connector succeeded")?;
        if !matches!(
            error.downcast_ref::<ConnectionError>(),
            Some(ConnectionError::Transport(Error::Status(
                Status::PEER_CLOSED
            )))
        ) {
            return Err(
                format!("connector confused transport failure with rejection: {error}").into(),
            );
        }
    }
    println!("COMPONENT_CONNECTION_ERRORS_OK");
    Ok(())
}
