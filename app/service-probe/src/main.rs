// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Native lifecycle/stdio probe. The plain mode deliberately uses only std I/O.
use std::{
    io::{Read, Write},
    time::Duration,
};

fn run() -> Result<(), Box<dyn std::error::Error>> {
    match std::env::args().nth(1).as_deref() {
        Some("plain") => {
            let mut byte = [0];
            if std::io::stdin().read(&mut byte)? != 0 {
                return Err("service stdin is not EOF".into());
            }
            println!("SERVICE_STDOUT_OK");
            eprintln!("SERVICE_STDERR_OK");
        }
        Some("journal-burst") => {
            let label = std::env::args()
                .nth(2)
                .ok_or("missing journal source label")?;
            let payload = |stream: &str| {
                let mut text: String = (0..96)
                    .map(|i| format!("JOURNAL_BURST {label} {stream} {i:03}\n"))
                    .collect();
                text.push_str(&format!("JOURNAL_BURST {label} {stream} EOF"));
                text
            };
            let output = payload("out");
            let errors = payload("err");
            let writer = std::thread::spawn(move || -> std::io::Result<()> {
                let mut stdout = std::io::stdout().lock();
                stdout.write_all(output.as_bytes())?;
                stdout.flush()
            });
            let mut stderr = std::io::stderr().lock();
            stderr.write_all(errors.as_bytes())?;
            stderr.flush()?;
            writer.join().map_err(|_| "stdout writer failed")??;
        }
        Some("fail") => {
            eprintln!("SERVICE_FAILURE_ATTEMPT");
            return Err("intentional failure".into());
        }
        Some("stop-component" | "stop-component-then-report") => {
            use hyper_os::{capability_channel::*, handle::*};
            let mut startup = hyper_rt::process::startup()?;
            let receiver = CapabilityChannel::from_handle(startup.take(
                hyper_os::startup::StartupPurpose::<CapabilityChannelObject>::new(0x80ff_0011),
            )?);
            let mut bytes = [core::mem::MaybeUninit::uninit(); 8];
            let mut slots = [CapabilityReceiveSlot::new::<ProcessObject>(Rights::WAIT)];
            receiver.receive(
                hyper_os::time::deadline_after(Duration::from_secs(5))?.as_raw(),
                &mut bytes,
                &mut slots,
            )?;
            let process = slots[0]
                .take::<ProcessObject>()?
                .ok_or("missing observer")?;
            process.as_process_supervisor().wait_terminated(
                hyper_os::time::deadline_after(Duration::from_secs(5))?.as_raw(),
            )?;
            if std::env::args().nth(1).as_deref() == Some("stop-component-then-report") {
                hyper_component::connection::report_failure(
                    startup.borrow(hyper_component::CONNECTOR)?,
                    "worker failed after journal termination",
                )?;
            }
        }
        Some("fault") => {
            // SAFETY: This test deliberately traps in its own process. No
            // memory or other process's state is accessed by the instruction.
            unsafe {
                #[cfg(target_arch = "aarch64")]
                core::arch::asm!("brk #0", options(noreturn));
                #[cfg(target_arch = "riscv64")]
                core::arch::asm!("ebreak", options(noreturn));
                #[cfg(target_arch = "x86_64")]
                core::arch::asm!("ud2", options(noreturn));
            }
        }
        Some("child-failure") => {
            let startup = hyper_rt::process::startup()?;
            hyper_component::connection::report_failure(
                startup.borrow(hyper_component::CONNECTOR)?,
                "test worker terminated: ProcessExited { status: 37 }",
            )?;
        }
        Some("terminal-disconnect" | "terminal-shell-first") => {
            use hyper_os::{Error, Status, stream, wait::wait_many};
            let _startup = hyper_rt::process::startup()?;
            // The runtime owns standard streams after claiming startup. Its
            // borrowed connectors are the same ones used by ordinary std I/O.
            let input = hyper_rt::process::stdin_stream()?.ok_or("missing input connector")?;
            let output = hyper_rt::process::stdout_stream()?.ok_or("missing output connector")?;
            let mut input = stream::connect(input.as_handle_ref(), stream::ACCESS_READ)?;
            let mut output = stream::connect(output.as_handle_ref(), stream::ACCESS_WRITE)?;
            input.start_read(1, hyper_os::DEADLINE_INFINITE)?;
            let mut byte = [0];
            if input.try_complete(&mut byte)?.is_some() {
                return Err("terminal read was not pending before failure".into());
            }
            let request = if std::env::args().nth(1).as_deref() == Some("terminal-shell-first") {
                "shell-first"
            } else {
                "stop terminal"
            };
            std::fs::write("/tmp/terminal-fault-request.tmp", request)?;
            std::fs::rename(
                "/tmp/terminal-fault-request.tmp",
                "/tmp/terminal-fault-request",
            )?;
            wait_many(
                &[input.wait_item()],
                hyper_os::time::deadline_after(Duration::from_secs(5))?.as_raw(),
            )?;
            if !matches!(
                input.try_complete(&mut byte),
                Err(Error::Status(Status::PEER_CLOSED))
            ) {
                return Err("terminal read did not report PEER_CLOSED".into());
            }
            if !matches!(
                output.write_all(b"must not reach the new terminal"),
                Err(Error::Status(Status::PEER_CLOSED))
            ) {
                return Err("old terminal output did not report PEER_CLOSED".into());
            }
            // Saving after a short delay proves the session owner did not
            // immediately kill applications when its terminal failed.
            std::thread::sleep(Duration::from_millis(250));
            std::fs::write(
                "/tmp/terminal-disconnect-proof",
                b"read=peer-closed; write=peer-closed; saved-before-retirement\n",
            )?;
        }
        Some("memory-lock") => {
            let startup = hyper_rt::process::startup()?;
            let root = startup.borrow(hyper_os::startup::ROOT_VMAR)?;
            let result = hyper_os::memory::lock_current_memory(root);
            match std::env::args().nth(2).as_deref() {
                Some("allowed") => {
                    result?;
                    hyper_os::memory::unlock_current_memory(root)?;
                    println!("COMPONENT_MEMORY_LOCK_ALLOWED_OK");
                }
                Some("denied") => {
                    if !matches!(
                        result,
                        Err(hyper_os::Error::Status(hyper_os::Status::ACCESS_DENIED))
                    ) {
                        return Err("resident=false did not deny memory locking".into());
                    }
                    println!("COMPONENT_MEMORY_LOCK_DENIED_OK");
                }
                _ => return Err("expected memory-lock allowed or denied".into()),
            }
        }
        Some("echo") => {
            let mut startup = hyper_rt::process::startup()?;
            let outgoing = hyper_os::capability_channel::CapabilityChannel::from_handle(
                startup.take(hyper_component::OUTGOING)?,
            );
            hyper_component::connection::publish(
                startup.borrow(hyper_component::CONNECTOR)?,
                "echo",
            )?;
            println!("COMPONENT_ECHO_PUBLISHED");
            let active = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
            loop {
                let (name, session) =
                    hyper_component::connection::accept(&outgoing, hyper_os::DEADLINE_INFINITE)?;
                if name != "echo" || active.load(std::sync::atomic::Ordering::Acquire) >= 8 {
                    continue;
                }
                active.fetch_add(1, std::sync::atomic::Ordering::AcqRel);
                let active = active.clone();
                std::thread::spawn(move || {
                    if let Ok(call) =
                        hyper_service_ipc::receive::<String>(&session, hyper_os::DEADLINE_INFINITE)
                    {
                        let reply = call.request.clone();
                        let _ = call.respond(&reply);
                    }
                    active.fetch_sub(1, std::sync::atomic::Ordering::AcqRel);
                });
            }
        }
        Some("connect") => {
            let startup = hyper_rt::process::startup()?;
            let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
            let mut workers = Vec::new();
            for n in 0..8 {
                let connector = startup
                    .borrow(hyper_component::CONNECTOR)?
                    .duplicate(hyper_service_ipc::CLIENT_RIGHTS)?;
                let barrier = barrier.clone();
                workers.push(std::thread::spawn(move || -> hyper_os::Result<()> {
                    barrier.wait();
                    let session = hyper_component::connection::connect(
                        connector.as_handle_ref(),
                        "echo",
                        hyper_service_ipc::deadline()?,
                    )?;
                    let value = format!("connection-{n}");
                    let reply: String =
                        hyper_service_ipc::request(session.as_handle_ref(), &value)?;
                    if reply != value {
                        return Err(hyper_os::Error::InvalidResponse);
                    }
                    Ok(())
                }));
            }
            for worker in workers {
                worker.join().map_err(|_| "client thread failed")??;
            }
            if !matches!(
                hyper_component::connection::connect(
                    startup.borrow(hyper_component::CONNECTOR)?,
                    "secret",
                    hyper_service_ipc::deadline()?,
                ),
                Err(hyper_component::connection::ConnectionError::Rejected(
                    hyper_component::connection::Rejection {
                        kind: hyper_component::connection::RejectionKind::NotOffered,
                        ..
                    }
                ))
            ) {
                return Err("undeclared capability did not return NotOffered".into());
            }
            println!("COMPONENT_CONCURRENT_CONNECT_OK");
            println!("COMPONENT_UNDECLARED_DENIED_OK");
        }
        Some("idle") => loop {
            std::thread::sleep(Duration::from_secs(1));
        },
        Some("timeout") => {
            let startup = hyper_rt::process::startup()?;
            let until = hyper_os::time::deadline_after(Duration::from_millis(500))?.as_raw();
            if hyper_component::connection::connect(
                startup.borrow(hyper_component::CONNECTOR)?,
                "echo",
                until,
            )
            .is_ok()
            {
                return Err("unpublished protocol was connected".into());
            }
            println!("COMPONENT_DEADLINE_OK");
        }
        Some("observe") => {
            let startup = hyper_rt::process::startup()?;
            let reply: hyper_component::Reply = hyper_service_ipc::request(
                startup.borrow(hyper_component::OBSERVE)?,
                &hyper_component::Request::Stop {
                    path: "/session-manager".into(),
                },
            )?;
            if reply.error.is_none() {
                return Err("read-only observer accepted a stop request".into());
            }
            println!("SERVICE_OBSERVER_DENIED_OK");
        }
        Some("ipc") => {
            let (listener, client) = hyper_os::capability_channel::CapabilityChannel::create()?;
            let worker = std::thread::spawn(move || -> hyper_os::Result<()> {
                let call = hyper_service_ipc::receive::<String>(
                    &listener,
                    hyper_service_ipc::deadline()?,
                )?;
                if call.request != "large reply" {
                    return Err(hyper_os::Error::InvalidResponse);
                }
                call.respond(&"x".repeat(96 * 1024))
            });
            let reply: String = hyper_service_ipc::request(client.as_handle_ref(), &"large reply")?;
            worker.join().map_err(|_| "IPC worker terminated")??;
            if reply.len() != 96 * 1024 || !reply.bytes().all(|b| b == b'x') {
                return Err("large reply framing lost bytes".into());
            }
            println!("SERVICE_LARGE_REPLY_OK");
        }
        _ => {
            return Err(
                "expected plain, journal-burst, fail, stop-component, fault, child-failure, terminal-disconnect, memory-lock, echo, connect, idle, timeout, observe, or ipc"
                    .into(),
            );
        }
    }
    Ok(())
}
fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("service-probe: {e}");
            std::process::ExitCode::FAILURE
        }
    }
}
