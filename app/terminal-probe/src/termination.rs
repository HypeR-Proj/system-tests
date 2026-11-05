// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Parent-supervised termination without terminal-control or stdin authority.

use std::io::{self, Write};
use std::time::{Duration, Instant};

use hyper_os::startup::Startup;
use hyper_service::process::control::{Client, Disposition, Signal};

use super::{Result, require};

pub fn run(startup: &mut Startup<'_>, mode: &str) -> Result<()> {
    let tag = std::env::args().nth(2).unwrap_or_else(|| "single".into());
    let exit_status = std::env::args()
        .nth(3)
        .map(|value| value.parse::<i32>())
        .transpose()?
        .unwrap_or(0);
    match mode {
        "terminate-default" => {
            report("DEFAULT_READY", &tag)?;
            // No control registration and no input reads. The supervisor must
            // remain able to apply its default action while this thread runs.
            let start = Instant::now();
            let mut value = 1u64;
            while start.elapsed() < Duration::from_secs(20) {
                for _ in 0..4096 {
                    value =
                        std::hint::black_box(value.wrapping_mul(1664525).wrapping_add(1013904223));
                }
            }
            return Err(io::Error::other("default termination was not applied").into());
        }
        "terminate-notify" | "terminate-ignore" => {
            let mut control = Client::take(startup)?
                .ok_or_else(|| io::Error::other("process control was not delegated"))?;
            let notify = mode == "terminate-notify";
            let disposition = if notify {
                Disposition::Notify
            } else {
                Disposition::Ignore
            };
            control.configure(disposition, deadline(10)?)?;
            report(
                if notify {
                    "NOTIFY_READY"
                } else {
                    "IGNORE_READY"
                },
                &tag,
            )?;
            if notify {
                let event = control.receive_until(deadline(20)?)?;
                require(
                    event.signal == Signal::Terminate,
                    "unexpected control event",
                )?;
                // The supervisor must allow this ordinary user code to finish.
                report("CLEANUP", &tag)?;
            } else {
                std::thread::sleep(Duration::from_secs(2));
                require(
                    control.try_receive()?.is_none(),
                    "ignored event was delivered",
                )?;
                report("SURVIVED", &tag)?;
            }
        }
        _ => return Err(io::Error::other("unknown termination probe mode").into()),
    }
    if exit_status != 0 {
        std::process::exit(exit_status);
    }
    Ok(())
}

fn deadline(seconds: u64) -> Result<u64> {
    Ok(hyper_os::time::monotonic_now()?
        .as_nanoseconds()
        .checked_add(seconds * 1_000_000_000)
        .ok_or_else(|| io::Error::other("termination probe deadline overflow"))?)
}

fn report(event: &str, tag: &str) -> Result<()> {
    // A pipeline consumer may already have exited; diagnostics deliberately
    // use stderr so reporting never depends on a downstream stdout reader.
    let line = format!("\nTERMINATE_{event}:{tag}\n");
    let mut error = io::stderr().lock();
    error.write_all(line.as_bytes())?;
    error.flush()?;
    Ok(())
}
