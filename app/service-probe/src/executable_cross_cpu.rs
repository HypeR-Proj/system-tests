// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Real CPU0 publication followed by CPU1 execution in an unprivileged child.

use crate::executable::{code, execute};
use hyper_os::{
    capability_channel::{CapabilityChannel, CapabilityDisposition, CapabilityReceiveSlot},
    fs::Directory,
    handle::{CapabilityChannelObject, OwnedHandle, ProcessObject, Rights, RightsOffer, VmoObject},
    memory::{ExecutableAuthority, ExecutableVmo, PAGE_SIZE, WritableVmo},
    startup::{self, StartupPurpose},
    task::{ProcessBuilder, ProcessTermination},
};
use hyper_service::process::{self, LoaderKind};
use std::{mem::MaybeUninit, time::Duration};

type Result<T = ()> = std::result::Result<T, Box<dyn std::error::Error>>;
const REPLY: StartupPurpose<CapabilityChannelObject> = StartupPurpose::new(0x80ff_0070);
const FIRST: StartupPurpose<VmoObject> = StartupPurpose::new(0x80ff_0071);
const SECOND: StartupPurpose<VmoObject> = StartupPurpose::new(0x80ff_0072);
const RX: Rights = Rights::READ
    .union(Rights::MAP)
    .union(Rights::EXECUTE)
    .union(Rights::INSPECT);

struct Child(OwnedHandle<ProcessObject>);
impl Drop for Child {
    fn drop(&mut self) {
        let _ = self.0.as_process_supervisor().request_stop();
        if let Ok(until) = hyper_os::time::deadline_after(Duration::from_secs(30)) {
            let _ = self
                .0
                .as_process_supervisor()
                .wait_terminated(until.as_raw());
        }
    }
}
impl Child {
    fn join(&self, until: u64) -> Result {
        self.0.as_process_supervisor().wait_terminated(until)?;
        let terminal = self.0.as_process_supervisor().info()?.terminal;
        if !matches!(
            terminal,
            Some(
                ProcessTermination::ProcessExited { status: 0 }
                    | ProcessTermination::LastThreadExited { status: 0 }
            )
        ) {
            return Err(format!("executable child failed: {terminal:?}").into());
        }
        Ok(())
    }
}
fn prepare(
    startup: &startup::Startup<'_>,
    mode: &str,
    cpu: u32,
    until: u64,
) -> Result<ProcessBuilder> {
    let root = Directory::from_handle(
        startup.borrow(startup::ROOT_DIRECTORY)?.duplicate(
            Rights::READ
                .union(Rights::EXECUTE)
                .union(Rights::PROTECT)
                .union(Rights::DUPLICATE)
                .union(Rights::TRANSFER),
        )?,
    );
    let image = root.open("/bin/service-probe", process::EXECUTABLE_FILE_RIGHTS)?;
    let loader = process::open_loader(&root, LoaderKind::Dynamic)?;
    let builder = ProcessBuilder::create(
        startup.borrow(startup::TASK_FACTORY)?,
        startup.borrow(startup::TASK_GROUP)?,
        startup.borrow(startup::RESOURCE_DOMAIN)?,
        image.as_handle_ref(),
        loader.as_handle_ref(),
    )?;
    builder.set_name(mode)?;
    builder.set_affinity(&[1u64 << cpu])?;
    for argument in ["/bin/service-probe", mode, &until.to_string()] {
        builder.add_argument(argument)?;
    }
    let libraries = process::open_library_directory(&root)?;
    builder.add_handle_duplicate(
        libraries.as_handle_ref(),
        startup::DYNAMIC_LIBRARY_DIRECTORY.as_raw(),
        RightsOffer::Exact(Rights::READ.union(Rights::EXECUTE).union(Rights::PROTECT)),
    )?;
    Ok(builder)
}
fn start(builder: ProcessBuilder) -> Result<Child> {
    builder.seal()?;
    Ok(Child(builder.start().map_err(|failure| {
        format!("start executable child: {:?}", failure.error())
    })?))
}

pub fn verify(startup: &startup::Startup<'_>, authority: &ExecutableAuthority) -> Result {
    let until = hyper_os::time::deadline_after(Duration::from_secs(30))?.as_raw();
    let builder = prepare(startup, "executable-publisher", 0, until)?;
    builder.add_handle_duplicate(
        authority.as_handle_ref(),
        startup::EXECUTABLE_AUTHORITY.as_raw(),
        RightsOffer::Exact(Rights::CREATE_EXECUTABLE),
    )?;
    let (receiver, sender) = CapabilityChannel::create()?;
    builder
        .add_handle_move(
            sender.into_handle(),
            REPLY.as_raw(),
            RightsOffer::Exact(Rights::WRITE.union(Rights::WAIT)),
        )
        .map_err(|failure| failure.error())?;
    let publisher = start(builder)?;
    let mut bytes = [MaybeUninit::uninit(); 1];
    let mut slots = [
        CapabilityReceiveSlot::new::<VmoObject>(RX.union(Rights::TRANSFER)),
        CapabilityReceiveSlot::new::<VmoObject>(RX.union(Rights::TRANSFER)),
    ];
    let message = receiver.receive(until, &mut bytes, &mut slots)?;
    if message.bytes() != [1] || message.capability_count() != 2 {
        return Err("invalid executable publication transfer".into());
    }
    let first = slots[0]
        .take::<VmoObject>()?
        .ok_or("missing first executable")?;
    let second = slots[1]
        .take::<VmoObject>()?
        .ok_or("missing second executable")?;
    publisher.join(until)?;
    let builder = prepare(startup, "executable-reader", 1, until)?;
    for (vmo, purpose) in [(first, FIRST), (second, SECOND)] {
        builder
            .add_handle_move(vmo, purpose.as_raw(), RightsOffer::Exact(RX))
            .map_err(|failure| failure.error())?;
    }
    let reader = start(builder)?;
    reader.join(until)?;
    println!("EXECUTABLE_CROSS_CPU_0_TO_1_OK");
    Ok(())
}

pub fn publisher() -> Result {
    let mut startup = hyper_rt::process::startup()?;
    let authority = ExecutableAuthority::from_handle(startup.take(startup::EXECUTABLE_AUTHORITY)?);
    let sender = CapabilityChannel::from_handle(startup.take(REPLY)?);
    let until = std::env::args()
        .nth(2)
        .ok_or("missing publication deadline")?
        .parse::<u64>()?;
    let source = WritableVmo::create(PAGE_SIZE)?;
    source.write_all_at(0, &code(42))?;
    let mut first = Some(authority.publish(source.as_handle_ref())?.into_handle());
    source.write_all_at(0, &code(43))?;
    let mut second = Some(authority.publish(source.as_handle_ref())?.into_handle());
    hyper_service_ipc::send(
        &sender,
        until,
        &[1],
        &mut [
            CapabilityDisposition::move_handle(
                &mut first,
                RightsOffer::Exact(RX.union(Rights::TRANSFER)),
            )?,
            CapabilityDisposition::move_handle(
                &mut second,
                RightsOffer::Exact(RX.union(Rights::TRANSFER)),
            )?,
        ],
    )?;
    Ok(())
}

pub fn reader() -> Result {
    let mut startup = hyper_rt::process::startup()?;
    if startup
        .take_optional(startup::EXECUTABLE_AUTHORITY)?
        .is_some()
    {
        return Err("CPU1 reader inherited publication authority".into());
    }
    let first = ExecutableVmo::from_handle(startup.take(FIRST)?, PAGE_SIZE)?;
    let second = ExecutableVmo::from_handle(startup.take(SECOND)?, PAGE_SIZE)?;
    let root = startup.borrow(startup::ROOT_VMAR)?;
    // SAFETY: immutable copies contain the reviewed return-constant code. Both
    // private mappings stay alive throughout execution and have no external edits.
    let first = unsafe { first.map(root) }?;
    // SAFETY: same private immutable owner contract as the first mapping.
    let second = unsafe { second.map(root) }?;
    if execute(&first) != 42 || execute(&second) != 43 {
        return Err("cross-CPU executable contents/cache publication mismatch".into());
    }
    Ok(())
}
