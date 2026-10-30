// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Kill a process that retains the sole VM owner, while its parent holds only
//! a vCPU observer. The observer cannot keep the VM owner handle alive.

use super::*;
use core::mem::MaybeUninit;
use hyper_os::capability_channel::{
    CapabilityChannel, CapabilityDisposition, CapabilityReceiveSlot,
};
use hyper_os::handle::{CapabilityChannelObject, RightsOffer, VirtualMachineCreationLeaseObject};
use hyper_os::startup::StartupPurpose;
use hyper_os::task::ProcessBuilder;

const LEASE: StartupPurpose<VirtualMachineCreationLeaseObject> = StartupPurpose::new(0x1000);
const CHANNEL: StartupPurpose<CapabilityChannelObject> = StartupPurpose::new(0x1001);
const OBSERVER_RIGHTS: Rights = Rights::WAIT.union(Rights::INSPECT);

pub(super) fn parent(startup: &mut Startup<'_>) -> Result<()> {
    let (channel, child_channel) =
        CapabilityChannel::create().map_err(|error| format!("owner create channel: {error:?}"))?;
    let directory = startup
        .take_root_directory()
        .map_err(|error| format!("owner take root directory: {error:?}"))?;
    let executable = directory
        .open(
            "/init",
            hyper_os::fs::FileRights::READ
                .union(hyper_os::fs::FileRights::EXECUTE)
                .union(hyper_os::fs::FileRights::PROTECT)
                .union(hyper_os::fs::FileRights::DUPLICATE)
                .union(hyper_os::fs::FileRights::TRANSFER),
        )
        .map_err(|error| format!("owner open /init: {error:?}"))?;
    #[cfg(target_arch = "aarch64")]
    const LOADER: &str = "/lib/userspace-loader-hyper-aarch64";
    #[cfg(target_arch = "riscv64")]
    const LOADER: &str = "/lib/userspace-loader-hyper-riscv64";
    #[cfg(target_arch = "x86_64")]
    const LOADER: &str = "/lib/userspace-loader-hyper-x86_64";
    let loader = directory
        .open(
            LOADER,
            hyper_os::fs::FileRights::READ.union(hyper_os::fs::FileRights::EXECUTE),
        )
        .map_err(show)?;
    let builder = ProcessBuilder::create(
        startup
            .borrow(startup::TASK_FACTORY)
            .map_err(|error| format!("owner task factory: {error:?}"))?,
        startup
            .borrow(startup::TASK_GROUP)
            .map_err(|error| format!("owner task group: {error:?}"))?,
        startup
            .borrow(startup::RESOURCE_DOMAIN)
            .map_err(|error| format!("owner resource domain: {error:?}"))?,
        executable.as_handle_ref(),
        loader.as_handle_ref(),
    )
    .map_err(|error| format!("owner create process builder: {error:?}"))?;
    builder.set_name("vm-smoke-owner").map_err(show)?;
    builder.add_argument("/init").map_err(show)?;
    builder.add_argument("--owner-child").map_err(show)?;
    builder
        .add_handle_move(lease(startup)?, LEASE.as_raw(), RightsOffer::SameRights)
        .map_err(|error| show(error.error()))?;
    builder
        .add_handle_move(
            child_channel.into_handle(),
            CHANNEL.as_raw(),
            RightsOffer::SameRights,
        )
        .map_err(|error| show(error.error()))?;
    // Rust startup deliberately retains Console for std I/O. It is no longer
    // an unclaimed Startup handle; borrow its actual runtime owner instead.
    builder
        .add_handle_duplicate(
            hyper_rt::process::console()
                .map_err(|error| format!("borrow runtime console: {error:?}"))?
                .as_handle_ref(),
            startup::CONSOLE.as_raw(),
            RightsOffer::SameRights,
        )
        .map_err(|error| format!("inherit runtime console: {error:?}"))?;
    builder
        .seal()
        .map_err(|error| format!("owner seal process: {error:?}"))?;
    let process = builder
        .start()
        .map_err(|error| format!("owner start process: {:?}", error.error()))?;
    let mut bytes = [MaybeUninit::uninit(); 1];
    let mut slots = [CapabilityReceiveSlot::new::<VirtualCpuObject>(
        OBSERVER_RIGHTS,
    )];
    let message = channel
        .receive(deadline()?, &mut bytes, &mut slots)
        .map_err(show)?;
    if message.bytes() != b"O" {
        return Err("owner child protocol".into());
    }
    let vcpu = slots[0]
        .take::<VirtualCpuObject>()
        .map_err(show)?
        .ok_or("missing vCPU observer")?;
    let supervisor = process.as_process_supervisor();
    supervisor.request_stop().map_err(show)?;
    supervisor.wait_terminated(deadline()?).map_err(show)?;
    vm::wait_vcpu_terminated(vcpu.as_handle_ref(), deadline()?).map_err(show)?;
    let info = vm::vcpu_info(vcpu.as_handle_ref()).map_err(show)?;
    if info.terminal != Some(VirtualCpuTermination::Administrative) {
        return Err("owner loss did not stop vCPU".into());
    }
    Ok(())
}

pub(super) fn child(startup: &mut Startup<'_>) -> Result<()> {
    let lease = startup
        .take(LEASE)
        .map_err(|error| format!("child take VM lease: {error:?}"))?;
    let channel = CapabilityChannel::from_handle(
        startup
            .take(CHANNEL)
            .map_err(|error| format!("child take channel: {error:?}"))?,
    );
    let mut guest = Guest::create(startup, lease, 0)?;
    guest.marker(b'B')?;
    let Guest {
        machine: _machine,
        vcpu,
        serial: _serial,
        output: _output,
        buffered: _buffered,
    } = guest;
    let mut observer = Some(vcpu);
    let until = deadline()?;
    loop {
        let mut dispositions = [CapabilityDisposition::move_handle(
            &mut observer,
            RightsOffer::Exact(OBSERVER_RIGHTS),
        )
        .map_err(show)?];
        match channel.try_send(b"O", &mut dispositions) {
            Ok(()) => break,
            Err(hyper_os::Error::Status(hyper_os::Status::WOULD_BLOCK)) => {
                let observation = hyper_os::wait::wait_many(
                    &[hyper_os::wait::WaitItem::new(
                        channel.as_handle_ref(),
                        hyper_os::wait::ObjectSignals::PEER_RECEIVING.union(
                            hyper_os::wait::ObjectSignals::<CapabilityChannelObject>::PEER_CLOSED,
                        ),
                    )],
                    until,
                )
                .map_err(show)?;
                if hyper_os::wait::ObjectSignals::<CapabilityChannelObject>::PEER_CLOSED
                    .is_present_in(observation.observed)
                {
                    return Err("owner rendezvous peer closed".into());
                }
            }
            Err(error) => return Err(show(error)),
        }
    }
    // Parent cancels this process. Rust destructors must not be the mechanism
    // that requests VM stop or unregisters these shared output pages.
    std::thread::sleep(Duration::from_secs(20));
    Err("owner child was not cancelled".into())
}
