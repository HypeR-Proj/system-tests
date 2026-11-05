// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

use hyper_service::process::control::{Client, Disposition};
use std::time::Duration;

pub fn notify() -> Result<(), Box<dyn std::error::Error>> {
    let mut startup = hyper_rt::process::startup()?;
    let mut control = Client::take(&mut startup)?.ok_or("missing process control")?;
    control.configure(
        Disposition::Notify,
        hyper_os::time::deadline_after(Duration::from_secs(5))?.as_raw(),
    )?;
    println!("COMPONENT_NOTIFY_READY");
    control.receive_until(hyper_os::DEADLINE_INFINITE)?;
    // These are new requests after stopping has begun: retaining an old session
    // would not verify that the cleanup connector still reaches dependencies.
    let connector = startup.borrow(hyper_component::CONNECTOR)?;
    hyper_component::connection::status(connector, "cleanup in progress")?;
    let session = hyper_component::connection::connect(
        connector,
        "echo",
        hyper_os::time::deadline_after(Duration::from_secs(2))?.as_raw(),
    )?;
    let expected = "cleanup after terminate".to_string();
    let reply: String = hyper_service_ipc::request(session.as_handle_ref(), &expected)?;
    if reply != expected {
        return Err("cleanup dependency lost its reply".into());
    }
    println!("COMPONENT_NOTIFY_CLEANUP_OK");
    Ok(())
}

pub fn ignore() -> Result<(), Box<dyn std::error::Error>> {
    let mut startup = hyper_rt::process::startup()?;
    let mut control = Client::take(&mut startup)?.ok_or("missing process control")?;
    control.configure(
        Disposition::Ignore,
        hyper_os::time::deadline_after(Duration::from_secs(5))?.as_raw(),
    )?;
    println!("COMPONENT_IGNORE_READY");
    // Keep the configured channel alive. A later forced process stop is the
    // component manager's bounded shutdown policy, not Ignore falling to Default.
    control.receive_until(hyper_os::DEADLINE_INFINITE)?;
    Err("Ignore unexpectedly delivered a Terminate event".into())
}
