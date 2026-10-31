// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Real process death with a blocked peer, private memory and revocable evidence.

use hyper_os::{
    Error, Status, channel,
    fs::Directory,
    handle::{ByteChannelObject, OwnedHandle, ProcessObject, Rights, RightsOffer, TaskGroupObject},
    retirement::{CrashView, ProcessRetirement},
    startup::{self, Startup, StartupPurpose},
    task::{ProcessBuilder, ProcessTermination, create_task_group},
    wait::{ObjectSignals, WaitItem, wait_many},
};
use hyper_service::process::{self, LoaderKind};
use std::{
    error::Error as StdError,
    sync::atomic::{AtomicU32, Ordering},
    time::Duration,
};

type Result<T> = std::result::Result<T, Box<dyn StdError>>;
const CHILD_CHANNEL: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x80ff_0020);
const LIMIT: u64 = 8 * 1024 * 1024;
static EXIT_BARRIER: AtomicU32 = AtomicU32::new(0);

unsafe extern "C" {
    fn hyper_thread_exit(status: i64) -> !;
}

extern "C" fn exit_together(_: u64) -> ! {
    EXIT_BARRIER.fetch_add(1, Ordering::AcqRel);
    while EXIT_BARRIER.load(Ordering::Acquire) != 2 {
        std::hint::spin_loop();
    }
    // SAFETY: This isolated child intentionally abandons runtime cleanup. Its
    // process owns both stacks until the kernel observes the final thread exit.
    unsafe { hyper_thread_exit(0) }
}

fn deadline() -> hyper_os::Result<u64> {
    Ok(hyper_os::time::deadline_after(Duration::from_secs(10))?.as_raw())
}

struct Child {
    process: OwnedHandle<ProcessObject>,
    group: OwnedHandle<TaskGroupObject>,
    channel: OwnedHandle<ByteChannelObject>,
    control: Option<ProcessRetirement>,
    stack: u64,
    heap: u64,
}

impl Drop for Child {
    fn drop(&mut self) {
        // An assertion failure must not strand either a live child or its scene.
        self.control.take();
        let _ = self.group.as_task_group().request_stop();
    }
}

fn builder(
    startup: &Startup<'_>,
    root: &Directory,
) -> Result<(ProcessBuilder, OwnedHandle<TaskGroupObject>)> {
    let factory = startup.borrow(startup::TASK_FACTORY)?;
    let domain = startup.borrow(startup::RESOURCE_DOMAIN)?;
    let group = create_task_group(factory, domain)?;
    let image = root.open("/bin/retirement-probe", process::EXECUTABLE_FILE_RIGHTS)?;
    let loader = process::open_loader(root, LoaderKind::Dynamic)?;
    let builder = ProcessBuilder::create(
        factory,
        group.as_handle_ref(),
        domain,
        image.as_handle_ref(),
        loader.as_handle_ref(),
    )?;
    builder.set_name("retirement-child")?;
    builder.set_affinity(&[1])?;
    builder.add_argument("/bin/retirement-probe")?;
    builder.add_argument("child")?;
    let libraries = process::open_library_directory(root)?;
    builder.add_handle_duplicate(
        libraries.as_handle_ref(),
        startup::DYNAMIC_LIBRARY_DIRECTORY.as_raw(),
        RightsOffer::Exact(Rights::READ.union(Rights::EXECUTE).union(Rights::PROTECT)),
    )?;
    Ok((builder, group))
}

fn start(startup: &Startup<'_>, root: &Directory, timeout: Duration, limit: u64) -> Result<Child> {
    let (builder, group) = builder(startup, root)?;
    if !matches!(
        builder.retain_on_exit(limit, 0),
        Err(Error::Status(Status::INVALID_ARGUMENT))
    ) {
        return Err("invalid retention deadline was accepted".into());
    }
    // A rejected mutator must leave the same builder retryable.
    let control = builder.retain_on_exit(limit, timeout.as_nanos() as u64)?;
    let (channel, peer) = channel::create_pair()?;
    builder
        .add_handle_move(
            peer,
            CHILD_CHANNEL.as_raw(),
            RightsOffer::Exact(Rights::READ.union(Rights::WRITE).union(Rights::WAIT)),
        )
        .map_err(|failure| failure.error())?;
    builder.seal()?;
    let process = builder.start().map_err(|failure| failure.error())?;
    let mut child = Child {
        process,
        group,
        channel,
        control: Some(control),
        stack: 0,
        heap: 0,
    };
    wait_many(
        &[WaitItem::new(
            child.channel.as_handle_ref(),
            ObjectSignals::<ByteChannelObject>::READABLE
                .union(ObjectSignals::<ByteChannelObject>::PEER_CLOSED),
        )],
        deadline()?,
    )?;
    let mut addresses = [0; 16];
    if child
        .channel
        .as_byte_channel()
        .try_receive(&mut addresses)?
        != 16
    {
        return Err("missing child memory locations".into());
    }
    child.stack = u64::from_le_bytes(addresses[..8].try_into()?);
    child.heap = u64::from_le_bytes(addresses[8..].try_into()?);
    Ok(child)
}

fn stop(child: &Child, fault: bool) -> Result<CrashView> {
    if fault {
        child.channel.as_byte_channel().send(b"fault")?;
    } else {
        child.process.as_process_supervisor().request_stop()?;
    }
    let view = stopped_scene(child)?;
    if fault && (view.info()?.fault_pc == 0 || view.info()?.fault_class == 0) {
        return Err("fault identity was lost".into());
    }
    Ok(view)
}

fn stopped_scene(child: &Child) -> Result<CrashView> {
    child
        .process
        .as_process_supervisor()
        .wait_terminated(deadline()?)?;
    let control = child.control.as_ref().ok_or("missing controller")?;
    let info = control.wait_ready(deadline()?)?;
    if !info.ready()
        || info.thread_count == 0
        || info.retained_bytes == 0
        || info.retained_bytes > info.memory_limit
    {
        return Err(format!("unexpected scene: {info:?}").into());
    }
    wait_many(
        &[WaitItem::new(
            child.channel.as_handle_ref(),
            ObjectSignals::<ByteChannelObject>::PEER_CLOSED,
        )],
        deadline()?,
    )?;
    if !matches!(
        child.channel.as_byte_channel().try_send(b"after-stop"),
        Err(Error::Status(Status::PEER_CLOSED))
    ) {
        return Err("retention kept the child's endpoint alive".into());
    }
    let view = control.open_view()?;
    let (thread, _) = view.thread(0)?;
    if thread.thread_koid == 0 || thread.pc == 0 || thread.sp == 0 {
        return Err("register snapshot missing".into());
    }
    view.mapping(0)?;
    view.handle(0)?;
    Ok(view)
}

fn verify_memory(view: &CrashView, address: u64, count: usize, byte: u8) -> Result<()> {
    let mut output = vec![0; count];
    let mut offset = 0;
    while offset < count {
        let n = view.read_memory(address + offset as u64, &mut output[offset..])?;
        if n == 0 {
            return Err("memory read made no progress".into());
        }
        offset += n;
    }
    if output.iter().any(|value| *value != byte) {
        return Err("retained bytes changed".into());
    }
    Ok(())
}

fn revoked(view: &CrashView) -> Result<()> {
    if !matches!(view.info(), Err(Error::Status(Status::REVOKED))) {
        return Err("view survived scene release".into());
    }
    if !matches!(
        view.read_memory(0, &mut [0; 1]),
        Err(Error::Status(Status::REVOKED))
    ) {
        return Err("memory access survived scene release".into());
    }
    Ok(())
}

fn retire(child: &Child) -> Result<()> {
    child.group.as_task_group().request_stop()?;
    child.group.as_task_group().wait_terminated(deadline()?)?;
    Ok(())
}

fn parent() -> Result<()> {
    let mut startup = hyper_rt::process::startup()?;
    let root = Directory::from_handle(startup.take(startup::ROOT_DIRECTORY)?);
    // Attenuation cannot be undone by the builder or the new retention syscall.
    let factory = startup
        .borrow(startup::TASK_FACTORY)?
        .duplicate(Rights::CREATE_PROCESS)?;
    let group = create_task_group(
        startup.borrow(startup::TASK_FACTORY)?,
        startup.borrow(startup::RESOURCE_DOMAIN)?,
    )?;
    let image = root.open("/bin/retirement-probe", process::EXECUTABLE_FILE_RIGHTS)?;
    let loader = process::open_loader(&root, LoaderKind::Dynamic)?;
    let denied = ProcessBuilder::create(
        factory.as_handle_ref(),
        group.as_handle_ref(),
        startup.borrow(startup::RESOURCE_DOMAIN)?,
        image.as_handle_ref(),
        loader.as_handle_ref(),
    )?;
    if !matches!(
        denied.retain_on_exit(LIMIT, 1_000_000_000),
        Err(Error::Status(Status::ACCESS_DENIED))
    ) {
        return Err("ordinary factory gained retention authority".into());
    }
    drop(denied);
    println!("RETIREMENT_AUTHORITY_OK");

    let child = start(&startup, &root, Duration::from_secs(10), LIMIT)?;
    let observer = child.process.as_process_supervisor().observe()?;
    let view = stop(&child, false)?;
    verify_memory(&view, child.stack, 64, 0x59)?;
    verify_memory(&view, child.heap, 8192, 0xa7)?;
    let copy = CrashView::from_handle(
        view.as_handle_ref()
            .duplicate(Rights::READ.union(Rights::INSPECT))?,
    );
    println!("RETIREMENT_MEMORY_REGISTERS_AND_PEER_CLOSED_OK");
    let control = child.control.as_ref().ok_or("missing controller")?;
    control.release()?;
    control.release()?;
    revoked(&view)?;
    revoked(&copy)?;
    retire(&child)?;
    drop(observer);
    println!("RETIREMENT_RELEASE_REVOKES_ALL_VIEWS_OK");

    let mut child = start(&startup, &root, Duration::from_secs(10), LIMIT)?;
    let view = stop(&child, true)?;
    child.control.take();
    revoked(&view)?;
    retire(&child)?;
    println!("RETIREMENT_OWNER_LOSS_AND_FAULT_OK");

    let child = start(&startup, &root, Duration::from_secs(5), LIMIT)?;
    let view = stop(&child, false)?;
    wait_many(
        &[WaitItem::new(
            child
                .control
                .as_ref()
                .ok_or("missing controller")?
                .as_handle_ref(),
            ObjectSignals::RELEASED,
        )],
        deadline()?,
    )?;
    revoked(&view)?;
    retire(&child)?;
    println!("RETIREMENT_DEADLINE_OK");

    let child = start(&startup, &root, Duration::from_secs(10), 4096)?;
    let view = stop(&child, false)?;
    if view.info()?.retained_bytes > 4096 {
        return Err("memory budget exceeded".into());
    }
    child
        .control
        .as_ref()
        .ok_or("missing controller")?
        .release()?;
    retire(&child)?;
    println!("RETIREMENT_MEMORY_LIMIT_OK");

    // Separate CPUs race the final membership transitions without a process-
    // wide stop reason. At least the actual last thread must be preserved.
    for _ in 0..4 {
        let child = start(&startup, &root, Duration::from_secs(10), LIMIT)?;
        child.channel.as_byte_channel().send(b"exit")?;
        let view = stopped_scene(&child)?;
        if child.process.as_process_supervisor().info()?.terminal
            != Some(ProcessTermination::LastThreadExited { status: 0 })
        {
            return Err("concurrent normal exits did not complete normally".into());
        }
        child
            .control
            .as_ref()
            .ok_or("missing controller")?
            .release()?;
        revoked(&view)?;
        retire(&child)?;
    }
    println!("RETIREMENT_CONCURRENT_LAST_EXIT_OK");
    Ok(())
}

fn child() -> Result<()> {
    let mut startup = hyper_rt::process::startup()?;
    if startup.borrow(startup::TASK_FACTORY).is_ok() {
        return Err("child received supervisor authority".into());
    }
    let channel = startup.take(CHILD_CHANNEL)?;
    let stack = [0x59u8; 64];
    let heap = vec![0xa7u8; 8192];
    let mut message = [0; 16];
    message[..8].copy_from_slice(&(stack.as_ptr() as u64).to_le_bytes());
    message[8..].copy_from_slice(&(heap.as_ptr() as u64).to_le_bytes());
    channel.as_byte_channel().send(&message)?;
    let mut command = [0; 16];
    let length = channel.as_byte_channel().receive(&mut command)?;
    std::hint::black_box((&stack, &heap));
    if &command[..length] == b"exit" {
        // u128 ensures the Native ABI's 16-byte SP alignment. No Rust TLS or
        // allocator is accessed by the raw worker; it only uses atomics and
        // the installed C thread-exit substrate. The QEMU fixture has 4 CPUs.
        let worker_stack = vec![0u128; 4096];
        let top = worker_stack.as_ptr() as u64
            + (worker_stack.len() * core::mem::size_of::<u128>()) as u64;
        // SAFETY: The stack is disjoint, retained below for the whole process
        // lifetime, and exit_together never returns or accesses thread-local data.
        let worker = unsafe {
            hyper_os::thread::create_with_affinity(
                exit_together as *const () as u64,
                top,
                0,
                0,
                &[2],
            )?
        };
        std::mem::forget(worker_stack);
        hyper_os::thread::start(worker.as_handle_ref())?;
        exit_together(0);
    }
    // SAFETY: The isolated child deliberately raises its own user exception.
    unsafe {
        #[cfg(target_arch = "aarch64")]
        core::arch::asm!("brk #0", options(noreturn));
        #[cfg(target_arch = "riscv64")]
        core::arch::asm!("ebreak", options(noreturn));
        #[cfg(target_arch = "x86_64")]
        core::arch::asm!("ud2", options(noreturn));
    }
}

fn main() -> std::process::ExitCode {
    let result = if std::env::args().nth(1).as_deref() == Some("child") {
        child()
    } else {
        parent()
    };
    match result {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("RETIREMENT_FAIL: {error:?}");
            std::process::ExitCode::FAILURE
        }
    }
}
