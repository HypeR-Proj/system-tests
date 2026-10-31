// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Interactive terminal contract probe, included only in development images.

use std::io::{self, IsTerminal, Read, Write};
use std::process::ExitCode;

use hyper_terminal::{ReadPolicy, Settings, Terminal};

mod backpressure;
mod formatting;
mod lease_tree;

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("terminal-probe: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<()> {
    if std::env::args().nth(1).as_deref() == Some("lease-owner") {
        // The parent retains a supervisor and stops this observation fixture.
        loop {
            std::thread::park();
        }
    }
    let mut startup = hyper_rt::process::startup()?;
    let mut terminal = Terminal::take(&mut startup)?
        .ok_or_else(|| io::Error::other("terminal control was not delegated"))?;
    match std::env::args().nth(1).as_deref() {
        Some("raw") => {
            terminal.set_settings(Settings::raw())?;
            ready("RAW")?;
            let mut bytes = [0; 256];
            io::stdin().read_exact(&mut bytes)?;
            if bytes
                .iter()
                .enumerate()
                .any(|(index, byte)| usize::from(*byte) != index)
            {
                return Err(io::Error::other("raw bytes were transformed or lost").into());
            }
            println!("TERMINAL_RAW_OK");
        }
        Some("canonical") => {
            let settings = Settings {
                echo: false,
                ..Settings::default()
            };
            terminal.set_settings(settings)?;
            ready("CANONICAL")?;
            // A partial-line EOF must not create a second, synthetic EOF.
            let mut bytes = [0; 16];
            let mut input = io::stdin();
            let count = input.read(&mut bytes)?;
            require(&bytes[..count] == b"abc", "partial-line EOF")?;
            let count = input.read(&mut bytes)?;
            require(count == 0, "empty EOF")?;
            let count = input.read(&mut bytes)?;
            require(&bytes[..count] == b"next\n", "read after EOF")?;
            println!("TERMINAL_CANONICAL_OK");
        }
        Some("formatting") => formatting::run(&mut terminal)?,
        Some("timing") => {
            let mut settings = Settings::raw();
            settings.read_policy = ReadPolicy::Available;
            terminal.set_settings(settings)?;
            let mut byte = [0];
            require(
                matches!(io::stdin().read(&mut byte), Err(error) if error.kind() == io::ErrorKind::WouldBlock),
                "available read",
            )?;
            settings.read_policy = ReadPolicy::FirstByteTimeout {
                timeout_ns: 50_000_000,
            };
            terminal.set_settings(settings)?;
            require(
                matches!(io::stdin().read(&mut byte), Err(error) if error.kind() == io::ErrorKind::TimedOut),
                "first-byte timeout",
            )?;
            println!("TERMINAL_TIMING_OK");
        }
        Some("metadata") => {
            require(io::stdin().is_terminal(), "stdin metadata")?;
            require(io::stdout().is_terminal(), "stdout metadata")?;
            require(io::stderr().is_terminal(), "stderr metadata")?;
            println!("TERMINAL_METADATA_OK");
        }
        Some("flow") => {
            let mut settings = Settings::raw();
            settings.input.receive_flow_control = true;
            terminal.set_settings(settings)?;
            ready("FLOW")?;
            let mut byte = [0];
            io::stdin().read_exact(&mut byte)?;
            require(byte[0] == b'g', "flow test admission")?;
            // The peer has sent STOP. Control and input must still make progress
            // while this writer fills the bounded output queue.
            let writer = std::thread::spawn(|| -> io::Result<()> {
                let block = [b'.'; 4096];
                let mut output = io::stdout().lock();
                for _ in 0..32 {
                    output.write_all(&block)?;
                }
                output.write_all(b"\n")?;
                output.flush()
            });
            terminal.set_size(100, 30)?;
            require(terminal.size()? == (100, 30), "control under output pause")?;
            io::stdin().read_exact(&mut byte)?;
            require(byte[0] == b'r', "input under output pause")?;
            writer
                .join()
                .map_err(|_| io::Error::other("flow writer failed"))??;
            println!("TERMINAL_FLOW_OK");
        }
        Some("raw-exit") => {
            terminal.set_settings(Settings::raw())?;
            println!("TERMINAL_RAW_EXIT");
            // Intentionally leave restoration to the server's Process observer.
        }
        Some("control-drop") => {
            terminal.set_settings(Settings::raw())?;
            drop(terminal);
            drop(startup);
            ready("CONTROL_DROP")?;
            let mut byte = [0];
            io::stdin().read_exact(&mut byte)?;
            require(byte[0] == b'x', "input after control closure")?;
            println!("TERMINAL_CONTROL_DROP_OK");
        }
        Some("backpressure") => {
            backpressure::run(&mut terminal)?;
            println!("TERMINAL_BACKPRESSURE_OK");
        }
        Some("lease-tree") => {
            lease_tree::run(&mut startup, &mut terminal)?;
            println!("TERMINAL_LEASE_TREE_OK");
        }
        _ => {
            return Err(io::Error::other(
                "usage: terminal-probe raw|canonical|formatting|timing|metadata|flow|raw-exit|control-drop|backpressure|lease-tree",
            )
            .into());
        }
    }
    io::stdout().flush()?;
    Ok(())
}

fn ready(mode: &str) -> Result<()> {
    println!("TERMINAL_{mode}_READY");
    io::stdout().flush()?;
    Ok(())
}

fn require(condition: bool, operation: &str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(io::Error::other(operation.to_owned()).into())
    }
}
