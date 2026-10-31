// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Two guests exercise real mailbox and notification IRQs with event-driven waits.

use hyper_os::guest_io::{Mailbox, Notification, Operation};
use hyper_os::handle::{
    GuestMailboxObject, GuestNotificationObject, VirtualCpuObject, VirtualMachineObject,
};
use hyper_os::memory::WritableVmo;
use hyper_os::startup::{self, Startup};
use hyper_os::vm::{self, MmioCompletion, MmioOperation, MmioRequest};
use hyper_os::wait::{self, ObjectSignals, WaitItem};
use std::num::NonZeroU64;
use std::time::Duration;

const RAM: u64 = 0x4000_0000;
const RAM_BYTES: u64 = 2 * 1024 * 1024;
const REPORT: u64 = 0x0a03_0000;
type Result<T> = std::result::Result<T, String>;
core::arch::global_asm!(include_str!("io_guest.S"));
unsafe extern "C" {
    static hyper_io_guest_start: u8;
    static hyper_io_guest_end: u8;
}
fn show(error: impl std::fmt::Debug) -> String {
    format!("{error:?}")
}
fn deadline() -> Result<u64> {
    hyper_os::time::deadline_after(Duration::from_secs(15))
        .map(|value| value.as_raw())
        .map_err(show)
}
struct Guest {
    machine: hyper_os::OwnedHandle<VirtualMachineObject>,
    cpu: hyper_os::OwnedHandle<VirtualCpuObject>,
    memory: WritableVmo,
}
impl Guest {
    fn create(startup: &Startup<'_>, role: u64) -> Result<Self> {
        let memory = WritableVmo::create_contiguous(RAM_BYTES).map_err(show)?;
        let start = core::ptr::addr_of!(hyper_io_guest_start);
        let end = core::ptr::addr_of!(hyper_io_guest_end);
        let length = end
            .addr()
            .checked_sub(start.addr())
            .ok_or("I/O assembly label order")?;
        if length == 0 || length > 0x1f0000 {
            return Err("I/O assembly payload extent".into());
        }
        // SAFETY: Immutable linker labels delimit the assembly payload in this
        // executable. The checked slice is copied into separately owned RAM.
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
                guest_physical_base: RAM,
                memory_size: RAM_BYTES,
                vcpu_count: 1,
                architecture: vm::Architecture::Aarch64,
                platform_profile: vm::PlatformProfile::Aarch64Reference,
            },
        )
        .map_err(|error| show(error.error()))?;
        let grant = vm::create_guest_memory(memory.as_handle_ref()).map_err(show)?;
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
                entry: RAM,
                stack: RAM + RAM_BYTES,
                arguments: [role, platform.aarch64_gic_version, 0, 0],
            },
        )
        .map_err(show)?;
        vm::seal(pending.as_handle_ref()).map_err(show)?;
        let (machine, cpu) = vm::install(pending).map_err(|error| show(error.error()))?;
        vm::register_mmio(
            machine.as_handle_ref(),
            REPORT,
            4096,
            NonZeroU64::new(7).ok_or("device ID")?,
        )
        .map_err(show)?;
        Ok(Self {
            machine,
            cpu,
            memory,
        })
    }
    fn checkpoint(&self, offset: u64, value: u64) -> Result<MmioRequest> {
        let item = WaitItem::new(
            self.cpu.as_handle_ref(),
            ObjectSignals::<VirtualCpuObject>::MMIO_REQUEST
                .union(ObjectSignals::<VirtualCpuObject>::TERMINATED),
        );
        wait::wait_many(&[item], deadline()?).map_err(show)?;
        let request = vm::pending_mmio(self.cpu.as_handle_ref())
            .map_err(show)?
            .ok_or("I/O guest terminated without checkpoint")?;
        if request.device.get() != 7
            || request.address != REPORT + offset
            || request.width != 4
            || request.operation != MmioOperation::Write(value)
        {
            return Err(format!(
                "unexpected I/O checkpoint {request:?}, expected {offset:#x}/{value:#x}"
            ));
        }
        Ok(request)
    }
    fn resume(&self, request: MmioRequest) -> Result<()> {
        vm::complete_mmio(self.cpu.as_handle_ref(), request.id, MmioCompletion::Write).map_err(show)
    }
    fn stop(&self) -> Result<()> {
        vm::request_stop(self.machine.as_handle_ref()).map_err(show)?;
        vm::wait_terminated(self.machine.as_handle_ref(), deadline()?).map_err(show)?;
        self.memory.write_all_at(0, &[0]).map_err(show)
    }
}

pub(super) fn suite(startup: &Startup<'_>) -> Result<()> {
    let front = Guest::create(startup, 0)?;
    let back = Guest::create(startup, 1)?;
    let outcome: Result<()> = (|| {
        let mailbox =
            Mailbox::create(back.machine.as_handle_ref(), 0x0a01_0000, 41).map_err(show)?;
        let notification = Notification::create(
            front.machine.as_handle_ref(),
            back.machine.as_handle_ref(),
            0x0a00_0000,
            0x0a02_0000,
            40,
            42,
        )
        .map_err(show)?;
        mailbox.send(b"ping").map_err(show)?;
        if !matches!(
            mailbox.send(b"full"),
            Err(hyper_os::Error::Status(hyper_os::Status::WOULD_BLOCK))
        ) {
            return Err("mailbox lost bounded backpressure".into());
        }
        vm::start_vcpu(back.cpu.as_handle_ref()).map_err(show)?;
        vm::start_vcpu(front.cpu.as_handle_ref()).map_err(show)?;
        let back_ready = back.checkpoint(0, 0xb0)?;
        let front_ready = front.checkpoint(0, 0xf0)?;
        back.resume(back_ready)?;
        wait::wait_many(
            &[WaitItem::new(
                mailbox.as_handle_ref(),
                ObjectSignals::<GuestMailboxObject>::READABLE,
            )],
            deadline()?,
        )
        .map_err(show)?;
        if mailbox.receive(&mut [0; 2]).is_ok() {
            return Err("short mailbox receive consumed record".into());
        }
        let mut response = [0; 4];
        if mailbox.receive(&mut response).map_err(show)? != 4 || &response != b"pong" {
            return Err("mailbox failed read-copy rollback".into());
        }
        let back_replied = back.checkpoint(4, 0xb1)?;
        back.resume(back_replied)?;
        notification.control(Operation::Enable).map_err(show)?;
        front.resume(front_ready)?;
        let front_called = front.checkpoint(8, 0xf2)?;
        let _back_called = back.checkpoint(8, 0xb2)?;
        back.stop()?;
        wait::wait_many(
            &[WaitItem::new(
                notification.as_handle_ref(),
                ObjectSignals::<GuestNotificationObject>::PEER_CLOSED,
            )],
            deadline()?,
        )
        .map_err(show)?;
        if notification.control(Operation::Enable).is_ok() {
            return Err("closed notification reactivated".into());
        }
        if mailbox.send(b"late").is_ok() {
            return Err("stopped guest accepted mailbox send".into());
        }
        notification
            .control(Operation::RaiseConfigurationInterrupt)
            .map_err(show)?;
        front.resume(front_called)?;
        let _front_closed = front.checkpoint(12, 0xf3)?;
        println!("VM-SMOKE: mailbox IRQ, direct cross-VM kick/call and peer-loss IRQ PASS");
        Ok(())
    })();
    // Always retire both owners, including a failure while either guest is
    // parked inside an IRQ handler or deferred checkpoint.
    let back_stop = back.stop();
    let front_stop = front.stop();
    outcome?;
    back_stop?;
    front_stop?;
    Ok(())
}
