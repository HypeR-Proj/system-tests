// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

use hyper_os::handle::{
    OwnedHandle, Rights, VirtualCpuObject, VirtualMachineObject, VirtualSerialObject,
};
use hyper_os::memory::WritableVmo;
use hyper_os::startup::{self, Startup};
use hyper_os::virtual_serial::{self, Output};
use hyper_os::vm::{self, VirtualCpuTermination};
use hyper_vm_smoke::{BOOT_SENTINELS, RAM_BASE, RAM_BYTES};
use std::collections::VecDeque;
use std::process::ExitCode;
use std::time::Duration;

mod owner;

core::arch::global_asm!(include_str!("guest.S"));
unsafe extern "C" {
    static hyper_vm_smoke_guest_start: u8;
    static hyper_vm_smoke_guest_end: u8;
}

type Result<T> = std::result::Result<T, String>;
fn show(error: impl std::fmt::Debug) -> String {
    format!("{error:?}")
}
fn deadline() -> Result<u64> {
    hyper_os::time::deadline_after(Duration::from_secs(10))
        .map(|value| value.as_raw())
        .map_err(show)
}

fn payload() -> Result<&'static [u8]> {
    let start = core::ptr::addr_of!(hyper_vm_smoke_guest_start);
    let end = core::ptr::addr_of!(hyper_vm_smoke_guest_end);
    let length = end
        .addr()
        .checked_sub(start.addr())
        .ok_or("guest blob order")?;
    if length == 0 || length > RAM_BYTES as usize {
        return Err("guest blob size".into());
    }
    // SAFETY: Both linker labels enclose one immutable assembly section retained
    // for this executable's life. The checked extent excludes arbitrary pointers.
    let bytes = unsafe { core::slice::from_raw_parts(start, length) };
    if !hyper_vm_smoke::validate_payload(bytes) {
        return Err("invalid guest blob".into());
    }
    Ok(bytes)
}

struct Guest {
    machine: OwnedHandle<VirtualMachineObject>,
    vcpu: OwnedHandle<VirtualCpuObject>,
    serial: OwnedHandle<VirtualSerialObject>,
    output: Output,
    buffered: VecDeque<u8>,
}

impl Guest {
    fn create(
        startup: &Startup<'_>,
        lease: OwnedHandle<hyper_os::handle::VirtualMachineCreationLeaseObject>,
        mode: u64,
    ) -> Result<Self> {
        let serial =
            virtual_serial::create().map_err(|error| format!("create serial: {error:?}"))?;
        let output = Output::register(
            &serial,
            startup
                .borrow(startup::ROOT_VMAR)
                .map_err(|error| format!("create root VMAR: {error:?}"))?,
            0xd000_0000,
            WritableVmo::create(virtual_serial::BUFFER_BYTES)
                .map_err(|error| format!("create serial VMO: {error:?}"))?,
        )
        .map_err(|error| format!("register serial output: {error:?}"))?;
        let memory =
            WritableVmo::create(RAM_BYTES).map_err(|error| format!("create RAM VMO: {error:?}"))?;
        memory
            .write_all_at(0, payload()?)
            .map_err(|error| format!("copy guest payload: {error:?}"))?;
        let pending = vm::create(
            lease,
            vm::Configuration {
                guest_physical_base: RAM_BASE,
                memory_size: RAM_BYTES,
                vcpu_count: 1,
                architecture: vm::Architecture::Riscv64,
                platform_profile: vm::PlatformProfile::Riscv64Reference,
            },
        )
        .map_err(|error| format!("create pending VM mode {mode}: {:?}", error.error()))?;
        vm::set_memory(pending.as_handle_ref(), memory.as_handle_ref())
            .map_err(|error| format!("set guest memory: {error:?}"))?;
        vm::set_bootstrap(
            pending.as_handle_ref(),
            vm::VirtualCpuBootstrap {
                entry: RAM_BASE,
                stack: RAM_BASE + RAM_BYTES,
                arguments: [mode, 1_000_000, BOOT_SENTINELS[0], BOOT_SENTINELS[1]],
            },
        )
        .map_err(|error| format!("set guest bootstrap: {error:?}"))?;
        let binding = serial
            .duplicate(Rights::TRANSFER.union(Rights::ASSIGN_DEVICE))
            .map_err(|error| format!("duplicate serial binding: {error:?}"))?;
        vm::set_virtual_serial(pending.as_handle_ref(), binding)
            .map_err(|error| format!("set guest serial: {:?}", error.error()))?;
        vm::seal(pending.as_handle_ref()).map_err(|error| format!("seal VM: {error:?}"))?;
        let (machine, vcpu) =
            vm::install(pending).map_err(|error| format!("install VM: {:?}", error.error()))?;
        let info = vm::machine_info(machine.as_handle_ref())
            .map_err(|error| format!("inspect installed VM: {error:?}"))?;
        if info.architecture != vm::Architecture::Riscv64
            || info.platform_profile != vm::PlatformProfile::Riscv64Reference
        {
            return Err("VM profile round trip".into());
        }
        vm::start_vcpu(vcpu.as_handle_ref()).map_err(|error| format!("start vCPU: {error:?}"))?;
        Ok(Self {
            machine,
            vcpu,
            serial,
            output,
            buffered: VecDeque::new(),
        })
    }

    fn marker(&mut self, expected: u8) -> Result<()> {
        let until = deadline()
            .map_err(|error| format!("marker {} deadline: {error}", char::from(expected)))?;
        loop {
            if let Some(actual) = self.buffered.pop_front() {
                if hyper_vm_smoke::consume_marker(&[actual], expected) {
                    return Ok(());
                }
                return Err(format!(
                    "guest marker: expected {expected:#x}, got {actual:#x}"
                ));
            }
            let mut buffer = [0; 32];
            let count = self
                .output
                .try_read(&mut buffer)
                .map_err(|error| format!("read marker {}: {error:?}", char::from(expected)))?;
            if count != 0 {
                self.buffered.extend(&buffer[..count]);
                continue;
            }
            let observation = hyper_os::wait::wait_many(&[self.output.wait_item()], until)
                .map_err(|error| {
                    format!(
                        "wait marker {}: {error:?}; vcpu={:?}; machine={:?}",
                        char::from(expected),
                        vm::vcpu_info(self.vcpu.as_handle_ref()),
                        vm::machine_info(self.machine.as_handle_ref())
                    )
                })?;
            if hyper_os::wait::ObjectSignals::<VirtualSerialObject>::PEER_CLOSED
                .is_present_in(observation.observed)
            {
                // Closure can race the last publication. Drain its final bytes
                // before treating the stream as an incomplete guest protocol.
                let count = self
                    .output
                    .try_read(&mut buffer)
                    .map_err(|error| format!("read marker {}: {error:?}", char::from(expected)))?;
                if count == 0 {
                    return Err(format!(
                        "serial closed before marker {}; vcpu={:?}",
                        char::from(expected),
                        vm::vcpu_info(self.vcpu.as_handle_ref())
                    ));
                }
                self.buffered.extend(&buffer[..count]);
            }
        }
    }

    fn stop(&self, expected: VirtualCpuTermination) -> Result<()> {
        vm::request_stop(self.machine.as_handle_ref())
            .map_err(|error| format!("request VM stop: {error:?}"))?;
        vm::wait_terminated(self.machine.as_handle_ref(), deadline()?).map_err(|error| {
            format!(
                "wait VM retirement: {error:?}; vcpu={:?}; machine={:?}",
                vm::vcpu_info(self.vcpu.as_handle_ref()),
                vm::machine_info(self.machine.as_handle_ref())
            )
        })?;
        vm::wait_vcpu_terminated(self.vcpu.as_handle_ref(), deadline()?).map_err(|error| {
            format!(
                "wait vCPU termination: {error:?}; vcpu={:?}",
                vm::vcpu_info(self.vcpu.as_handle_ref())
            )
        })?;
        let info = vm::vcpu_info(self.vcpu.as_handle_ref())
            .map_err(|error| format!("inspect stopped vCPU: {error:?}"))?;
        if info.terminal != Some(expected) {
            return Err(format!("guest terminal: {:?}", info.terminal));
        }
        Ok(())
    }
}

fn lease(
    startup: &Startup<'_>,
) -> Result<OwnedHandle<hyper_os::handle::VirtualMachineCreationLeaseObject>> {
    vm::derive_creation_lease(
        startup
            .borrow(startup::VIRTUAL_MACHINE_CREATION_AUTHORITY)
            .map_err(show)?,
        startup.borrow(startup::RESOURCE_DOMAIN).map_err(show)?,
    )
    .map_err(show)
}

fn suite(startup: &mut Startup<'_>) -> Result<()> {
    println!("VM-SMOKE: administrative-stop START");
    {
        let authority = lease(startup)?;
        let unsupported = vm::platform_info(
            authority.as_handle_ref(),
            vm::PlatformProfile::Aarch64Reference,
        );
        if unsupported != Err(hyper_os::Error::Status(hyper_os::Status::NOT_SUPPORTED)) {
            return Err(format!("unsupported platform query: {unsupported:?}"));
        }
        let first = vm::platform_info(
            authority.as_handle_ref(),
            vm::PlatformProfile::Riscv64Reference,
        )
        .map_err(show)?;
        let second = vm::platform_info(
            authority.as_handle_ref(),
            vm::PlatformProfile::Riscv64Reference,
        )
        .map_err(show)?;
        if first != second
            || first.architecture != vm::Architecture::Riscv64
            || first.platform_profile != vm::PlatformProfile::Riscv64Reference
            || first.counter_frequency_hz == 0
        {
            return Err(format!("unstable platform metadata: {first:?}, {second:?}"));
        }
        // Inspection, including a rejected profile, must preserve the lease.
        let mut guest = Guest::create(startup, authority, 0)?;
        guest.marker(b'B')?;
        guest.stop(VirtualCpuTermination::Administrative)?;
        println!("VM-SMOKE: administrative-stop PASS");
    }
    println!("VM-SMOKE: timer-wfi-registers START");
    {
        let mut guest = Guest::create(startup, lease(startup)?, 1)?;
        guest.marker(b'T')?;
        guest.marker(b'D')?;
        guest.marker(b'S')?;
        guest.stop(VirtualCpuTermination::Administrative)?;
        println!("VM-SMOKE: timer-wfi-registers PASS");
    }
    println!("VM-SMOKE: serial-input-plic-wfi START");
    {
        let mut guest = Guest::create(startup, lease(startup)?, 2)?;
        guest.marker(b'R')?;
        std::thread::sleep(Duration::from_millis(30));
        if virtual_serial::try_write(guest.serial.as_handle_ref(), b"Z").map_err(show)? != 1 {
            return Err("input rejected".into());
        }
        guest.marker(b'Z')?;
        guest.marker(b'I')?;
        guest.stop(VirtualCpuTermination::Administrative)?;
        println!("VM-SMOKE: serial-input-plic-wfi PASS");
    }
    println!("VM-SMOKE: guest-fault START");
    {
        let mut guest = Guest::create(startup, lease(startup)?, 3)?;
        guest.marker(b'X')?;
        vm::wait_vcpu_terminated(guest.vcpu.as_handle_ref(), deadline()?).map_err(show)?;
        // Out-of-RAM data accesses go through device dispatch. No device
        // claims this address, so its terminal reason is MMIO, not a RAM fault.
        guest.stop(VirtualCpuTermination::Mmio)?;
        println!("VM-SMOKE: guest-fault PASS");
    }
    println!("VM-SMOKE: vu-privilege-delegation START");
    {
        let mut guest = Guest::create(startup, lease(startup)?, 4)?;
        guest.marker(b'U')?;
        guest.stop(VirtualCpuTermination::Administrative)?;
        println!("VM-SMOKE: vu-privilege-delegation PASS");
    }
    println!("VM-SMOKE: owner-cleanup START");
    owner::parent(startup)?;
    println!("VM-SMOKE: owner-cleanup PASS");
    for _ in 0..8 {
        let mut guest = Guest::create(startup, lease(startup)?, 0)?;
        guest.marker(b'B')?;
        guest.stop(VirtualCpuTermination::Administrative)?;
    }
    println!("VM-SMOKE: retirement-reuse PASS");
    Ok(())
}

pub(super) fn main() -> ExitCode {
    let outcome = hyper_rt::process::startup()
        .map_err(show)
        .and_then(|mut startup| {
            if std::env::args().nth(1).as_deref() == Some("--owner-child") {
                owner::child(&mut startup)
            } else {
                suite(&mut startup)
            }
        });
    match outcome {
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
