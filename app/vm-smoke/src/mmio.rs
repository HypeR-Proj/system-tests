// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Hardware-detached userspace MMIO acceptance, booted as Native /init.

use hyper_os::handle::{Rights, VirtualCpuObject};
use hyper_os::memory::WritableVmo;
use hyper_os::startup::{self, Startup};
use hyper_os::virtual_serial::{self, Output};
use hyper_os::vm::{self, MmioCompletion, MmioOperation};
use hyper_os::wait::{ObjectSignals, WaitSet};
use std::num::NonZeroU64;
use std::process::ExitCode;
use std::time::Duration;

type Result<T> = std::result::Result<T, String>;
const RAM_BASE: u64 = 0x4000_0000;
const RAM_BYTES: u64 = 2 * 1024 * 1024;
const MMIO_BASE: u64 = 0x0a00_0000;
core::arch::global_asm!(include_str!("mmio_guest.S"));
unsafe extern "C" {
    static hyper_mmio_guest_start: u8;
    static hyper_mmio_guest_end: u8;
}

fn show(error: impl std::fmt::Debug) -> String {
    format!("{error:?}")
}
fn deadline() -> Result<u64> {
    hyper_os::time::deadline_after(Duration::from_secs(10))
        .map(|d| d.as_raw())
        .map_err(show)
}
fn suite(startup: &Startup<'_>) -> Result<()> {
    shared_retirement(startup)?;
    super::io::suite(startup)?;
    for cancel in [false, true] {
        let serial = virtual_serial::create().map_err(show)?;
        let output = Output::register(
            &serial,
            startup.borrow(startup::ROOT_VMAR).map_err(show)?,
            0xd000_0000,
            WritableVmo::create(virtual_serial::BUFFER_BYTES).map_err(show)?,
        )
        .map_err(show)?;
        let mut serial_sink = Some((serial, output));
        let memory = WritableVmo::create_contiguous(RAM_BYTES).map_err(show)?;
        let start = core::ptr::addr_of!(hyper_mmio_guest_start);
        let end = core::ptr::addr_of!(hyper_mmio_guest_end);
        let length = end
            .addr()
            .checked_sub(start.addr())
            .ok_or("payload order")?;
        if length == 0 || length > RAM_BYTES as usize {
            return Err("payload length".into());
        }
        // SAFETY: Linker labels bound immutable assembly bytes retained for the
        // process lifetime. The checked extent is copied into an independent VMO.
        let bytes = unsafe { core::slice::from_raw_parts(start, length) };
        memory.write_all_at(0, bytes).map_err(show)?;
        let lease = vm::derive_creation_lease(
            startup
                .borrow(startup::VIRTUAL_MACHINE_CREATION_AUTHORITY)
                .map_err(show)?,
            startup.borrow(startup::RESOURCE_DOMAIN).map_err(show)?,
        )
        .map_err(show)?;
        let platform =
            vm::platform_info(lease.as_handle_ref(), vm::PlatformProfile::Aarch64Reference)
                .map_err(show)?;
        let pending = vm::create(
            lease,
            vm::Configuration {
                guest_physical_base: RAM_BASE,
                memory_size: RAM_BYTES,
                vcpu_count: 1,
                architecture: vm::Architecture::Aarch64,
                platform_profile: vm::PlatformProfile::Aarch64Reference,
            },
        )
        .map_err(|e| show(e.error()))?;
        let serial = &serial_sink.as_ref().ok_or("serial sink")?.0;
        vm::set_virtual_serial(
            pending.as_handle_ref(),
            serial
                .duplicate(Rights::TRANSFER.union(Rights::ASSIGN_DEVICE))
                .map_err(show)?,
        )
        .map_err(|e| show(e.error()))?;
        let shared = vm::create_guest_memory(memory.as_handle_ref()).map_err(show)?;
        if memory.write_all_at(0, bytes).is_ok() {
            return Err("Native writer survived hardware grant creation".into());
        }
        vm::map_guest_memory(
            pending.as_handle_ref(),
            shared.as_handle_ref(),
            0,
            0,
            RAM_BYTES / 2,
        )
        .map_err(show)?;
        if vm::map_guest_memory(pending.as_handle_ref(), shared.as_handle_ref(), 0, 0, 4096).is_ok()
        {
            return Err("overlapping guest memory region accepted".into());
        }
        vm::map_guest_memory(
            pending.as_handle_ref(),
            shared.as_handle_ref(),
            RAM_BYTES / 2,
            RAM_BYTES / 2,
            RAM_BYTES / 2,
        )
        .map_err(show)?;
        // Installed translations, not the grant handle, retain page ownership.
        drop(shared);

        vm::set_bootstrap(
            pending.as_handle_ref(),
            vm::VirtualCpuBootstrap {
                entry: RAM_BASE,
                stack: RAM_BASE + RAM_BYTES,
                arguments: [platform.aarch64_gic_version, 0, 0, 0],
            },
        )
        .map_err(show)?;
        vm::seal(pending.as_handle_ref()).map_err(show)?;
        let (machine, vcpu) = vm::install(pending).map_err(|e| show(e.error()))?;
        let outcome = (|| {
            let device = NonZeroU64::new(1).ok_or("device identity")?;
            vm::register_mmio(machine.as_handle_ref(), MMIO_BASE, 4096, device).map_err(show)?;
            let wait = WaitSet::new(1).map_err(show)?;
            let registration = wait
                .add(
                    vcpu.as_handle_ref(),
                    ObjectSignals::<VirtualCpuObject>::MMIO_REQUEST,
                )
                .map_err(show)?;
            vm::start_vcpu(vcpu.as_handle_ref()).map_err(show)?;
            let mut requests = 0;
            loop {
                let event = wait.wait(deadline()?).map_err(show)?;
                if event.registration != registration {
                    return Err("unexpected registration".into());
                }
                let request = vm::pending_mmio(vcpu.as_handle_ref())
                    .map_err(show)?
                    .ok_or("missing MMIO request")?;
                if vm::pending_mmio(vcpu.as_handle_ref()).map_err(show)? != Some(request) {
                    return Err("request inspection consumed the instruction".into());
                }
                if cancel {
                    vm::request_stop(machine.as_handle_ref()).map_err(show)?;
                    if vm::complete_mmio(vcpu.as_handle_ref(), request.id, MmioCompletion::Write)
                        .is_ok()
                    {
                        return Err("stopped VM accepted stale completion".into());
                    }
                    break;
                }
                if request.device != device {
                    return Err("device identity mismatch".into());
                }
                let offset = request
                    .address
                    .checked_sub(MMIO_BASE)
                    .ok_or("MMIO address")?;
                let completion = match (offset, request.width, request.operation) {
                    (24, 4, MmioOperation::Write(0x5342)) if requests == 0 => {
                        // The guest has filled both the output ring and UART
                        // FIFO. Closing the last consumer must drain TX and
                        // assert its interrupt without a further UART access.
                        drop(serial_sink.take().ok_or("serial sink already closed")?);
                        vm::complete_mmio(vcpu.as_handle_ref(), request.id, MmioCompletion::Write)
                            .map_err(show)?;
                        wait.rearm(registration).map_err(show)?;
                        continue;
                    }
                    (0, 4, MmioOperation::Write(0x1234)) => {
                        if vm::complete_mmio(
                            vcpu.as_handle_ref(),
                            request.id,
                            MmioCompletion::Read(0),
                        )
                        .is_ok()
                        {
                            return Err("write accepted read completion".into());
                        }
                        MmioCompletion::Write
                    }
                    (4, 1, MmioOperation::Read) => MmioCompletion::Read(0x80),
                    (8, 8, MmioOperation::Write(value)) if value == (-128_i64) as u64 => {
                        MmioCompletion::Write
                    }
                    (16, 4, MmioOperation::Read) => MmioCompletion::Read(0x1234),
                    (32, 4, MmioOperation::Write(0x55)) if requests == 128 => {
                        vm::complete_mmio(vcpu.as_handle_ref(), request.id, MmioCompletion::Abort)
                            .map_err(show)?;
                        break;
                    }
                    _ => return Err(format!("invalid guest MMIO: {request:?}")),
                };
                vm::complete_mmio(vcpu.as_handle_ref(), request.id, completion).map_err(show)?;
                if vm::complete_mmio(vcpu.as_handle_ref(), request.id, completion).is_ok() {
                    return Err("duplicate completion resumed instruction twice".into());
                }
                requests += 1;
                wait.rearm(registration).map_err(show)?;
            }
            Ok(())
        })();
        vm::request_stop(machine.as_handle_ref()).map_err(show)?;
        vm::wait_terminated(machine.as_handle_ref(), deadline()?).map_err(show)?;
        outcome?;
    }
    println!("VM-SMOKE: userspace-MMIO and pending-stop PASS");
    Ok(())
}

/// Retiring one VM must not release another VM's hardware-write admission.
fn shared_retirement(startup: &Startup<'_>) -> Result<()> {
    let memory = WritableVmo::create_contiguous(RAM_BYTES).map_err(show)?;
    let grant = vm::create_guest_memory(memory.as_handle_ref()).map_err(show)?;
    let mut machines = Vec::new();
    for _ in 0..2 {
        let lease = vm::derive_creation_lease(
            startup
                .borrow(startup::VIRTUAL_MACHINE_CREATION_AUTHORITY)
                .map_err(show)?,
            startup.borrow(startup::RESOURCE_DOMAIN).map_err(show)?,
        )
        .map_err(show)?;
        let pending = vm::create(
            lease,
            vm::Configuration {
                guest_physical_base: RAM_BASE,
                memory_size: RAM_BYTES,
                vcpu_count: 1,
                architecture: vm::Architecture::Aarch64,
                platform_profile: vm::PlatformProfile::Aarch64Reference,
            },
        )
        .map_err(|e| show(e.error()))?;
        vm::map_guest_memory(
            pending.as_handle_ref(),
            grant.as_handle_ref(),
            0,
            0,
            RAM_BYTES,
        )
        .map_err(show)?;
        vm::set_bootstrap(
            pending.as_handle_ref(),
            vm::VirtualCpuBootstrap {
                entry: RAM_BASE,
                stack: RAM_BASE + RAM_BYTES,
                arguments: [0; 4],
            },
        )
        .map_err(show)?;
        vm::seal(pending.as_handle_ref()).map_err(show)?;
        machines.push(vm::install(pending).map_err(|e| show(e.error()))?);
    }
    drop(grant);
    while let Some((machine, vcpu)) = machines.pop() {
        if memory.write_all_at(0, &[0x5a]).is_ok() {
            return Err("guest backing became writable before its last VM retired".into());
        }
        vm::request_stop(machine.as_handle_ref()).map_err(show)?;
        vm::wait_terminated(machine.as_handle_ref(), deadline()?).map_err(show)?;
        drop(vcpu);
        drop(machine);
    }
    memory.write_all_at(0, &[0x5a]).map_err(show)?;
    println!("VM-SMOKE: shared backing last-owner retirement PASS");
    Ok(())
}

pub(super) fn main() -> ExitCode {
    // Startup owns the initial capability set, including the output channel
    // and task group. Retain it until the suite result has been reported.
    let startup = match hyper_rt::process::startup() {
        Ok(startup) => startup,
        Err(error) => {
            eprintln!("VM-SMOKE: FAIL {}", show(error));
            return ExitCode::FAILURE;
        }
    };
    match suite(&startup) {
        Ok(()) => {
            println!("VM-SMOKE: PASS");
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("VM-SMOKE: FAIL {error}");
            ExitCode::FAILURE
        }
    }
}
