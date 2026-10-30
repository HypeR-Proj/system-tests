// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Native /init acceptance fixture. No fault injection enters product services.

use hyper_os::{
    capability_channel::{CapabilityChannel, CapabilityDisposition, CapabilityReceiveSlot},
    fs::{Directory, DirectoryRights, FileRights},
    handle::{
        ByteChannelObject, CapabilityChannelObject, FileObject, OwnedHandle, ProcessObject, Rights,
        RightsOffer, VmarObject, VmoObject,
    },
    memory::{PrivateMappingMode, PrivateMappingOptions},
    startup::{self, Startup, StartupPurpose},
    task::{ProcessBuilder, ProcessPhase, ProcessTermination},
    wait::{self, ObjectSignals, WaitItem},
};
use hyper_service::filesystem;
use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    mem::MaybeUninit,
    time::Duration,
};

mod directory;
mod directory_volume;
mod volume;

type Result<T> = std::result::Result<T, String>;

const NOTICE: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x1000);
const PROTECTED_FILE: StartupPurpose<FileObject> = StartupPurpose::new(0x1001);
const EXPORTED_VMAR: StartupPurpose<CapabilityChannelObject> = StartupPurpose::new(0x1002);

fn show(error: impl std::fmt::Debug) -> String {
    format!("{error:?}")
}

fn context<E: std::fmt::Debug>(step: &'static str) -> impl FnOnce(E) -> String {
    move |error| format!("{step}: {error:?}")
}

fn deadline() -> Result<u64> {
    hyper_os::time::deadline_after(Duration::from_secs(10))
        .map(|value| value.as_raw())
        .map_err(show)
}

fn receive(channel: &OwnedHandle<ByteChannelObject>, expected: &[u8]) -> Result<()> {
    wait::wait_many(
        &[WaitItem::new(
            channel.as_handle_ref(),
            ObjectSignals::<ByteChannelObject>::READABLE
                .union(ObjectSignals::<ByteChannelObject>::PEER_CLOSED),
        )],
        deadline()?,
    )
    .map_err(show)?;
    let mut bytes = [0; 64];
    let length = channel
        .as_byte_channel()
        .try_receive(&mut bytes)
        .map_err(show)?;
    if &bytes[..length] != expected {
        return Err(format!("unexpected worker message: {:?}", &bytes[..length]));
    }
    Ok(())
}

struct Worker {
    process: OwnedHandle<ProcessObject>,
    owner: Option<OwnedHandle<ByteChannelObject>>,
    notice: OwnedHandle<ByteChannelObject>,
    control: CapabilityChannel,
    transport: Option<(OwnedHandle<ByteChannelObject>, OwnedHandle<VmoObject>)>,
}

impl Worker {
    fn launch(startup: &Startup<'_>, root: &Directory, path: &str) -> Result<Self> {
        root.create_directory(path, 0o755).map_err(show)?;
        let image = root
            .open("/init", hyper_service::process::EXECUTABLE_FILE_RIGHTS)
            .map_err(show)?;
        let environment = hyper_service::process::open_environment(root, "/").map_err(show)?;
        let loader = hyper_service::process::open_loader(
            &environment,
            hyper_service::process::LoaderKind::Bootstrap,
        )
        .map_err(show)?;
        let builder = ProcessBuilder::create(
            startup.borrow(startup::TASK_FACTORY).map_err(show)?,
            startup.borrow(startup::TASK_GROUP).map_err(show)?,
            startup.borrow(startup::RESOURCE_DOMAIN).map_err(show)?,
            image.as_handle_ref(),
            loader.as_handle_ref(),
        )
        .map_err(show)?;
        builder.set_name("fs-failure-worker").map_err(show)?;
        // The production server locks its mappings before publishing READY.
        builder.set_memory_lock_allowed(true).map_err(show)?;
        builder.add_argument("/init").map_err(show)?;
        builder.add_argument("--worker").map_err(show)?;
        builder
            .add_handle_duplicate(
                hyper_rt::process::console().map_err(show)?.as_handle_ref(),
                startup::CONSOLE.as_raw(),
                RightsOffer::SameRights,
            )
            .map_err(show)?;
        let (owner, child_owner) = hyper_os::channel::create_pair().map_err(show)?;
        let (control, child_control) = CapabilityChannel::create().map_err(show)?;
        builder
            .add_handle_move(
                child_control.into_handle(),
                filesystem::WORKER_CONTROL.as_raw(),
                RightsOffer::Exact(Rights::READ.union(Rights::WRITE).union(Rights::WAIT)),
            )
            .map_err(|error| show(error.error()))?;
        let (notice, child_notice) = hyper_os::channel::create_pair().map_err(show)?;
        for (handle, purpose, rights) in [
            (
                child_owner,
                filesystem::OWNER,
                Rights::WAIT.union(Rights::READ),
            ),
            (
                child_notice,
                NOTICE,
                Rights::WAIT.union(Rights::WRITE).union(Rights::READ),
            ),
        ] {
            builder
                .add_handle_move(handle, purpose.as_raw(), RightsOffer::Exact(rights))
                .map_err(|error| show(error.error()))?;
        }
        builder.seal().map_err(show)?;
        let process = builder.start().map_err(|error| show(error.error()))?;
        let mut worker = Self {
            process,
            owner: Some(owner),
            notice,
            control,
            transport: None,
        };
        let mut bytes = [MaybeUninit::uninit(); 16];
        let mut slots = [
            CapabilityReceiveSlot::new::<ByteChannelObject>(
                Rights::READ.union(Rights::WRITE).union(Rights::WAIT),
            ),
            CapabilityReceiveSlot::new::<VmoObject>(
                Rights::READ.union(Rights::WRITE).union(Rights::MAP),
            ),
        ];
        let message = worker
            .control
            .receive(deadline()?, &mut bytes, &mut slots)
            .map_err(show)?;
        if message.bytes() != filesystem::WORKER_READY || message.capability_count() != 2 {
            return Err("invalid fixture worker handshake".into());
        }
        let channel = slots[0]
            .take::<ByteChannelObject>()
            .map_err(show)?
            .ok_or("missing worker endpoint")?;
        let buffer = slots[1]
            .take::<VmoObject>()
            .map_err(show)?
            .ok_or("missing worker buffer")?;
        hyper_os::filesystem::mount(
            channel.as_handle_ref(),
            buffer.as_handle_ref(),
            None,
            root.as_handle_ref(),
            path,
            false,
        )
        .map_err(show)?;
        worker.transport = Some((channel, buffer));
        Ok(worker)
    }

    fn stop(&self) -> Result<()> {
        self.process
            .as_process_supervisor()
            .request_stop()
            .map_err(show)?;
        self.wait_retired()
    }

    fn wait_retired(&self) -> Result<()> {
        let supervisor = self.process.as_process_supervisor();
        let limit = deadline()?;
        supervisor.wait_terminated(limit).map_err(show)?;
        // TERMINATED means execution stopped. The reaper closes the worker's
        // RPC endpoint afterward; assert cache retirement only once that handle
        // teardown has completed, without depending on which CPU ran first.
        while supervisor.info().map_err(show)?.phase != ProcessPhase::Retired {
            if hyper_os::time::monotonic_now()
                .map_err(show)?
                .as_nanoseconds()
                >= limit
            {
                return Err("worker resource retirement timed out".into());
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        Ok(())
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        // Cleanup also cancels a worker when an assertion returns early.
        let _ = self.process.as_process_supervisor().request_stop();
    }
}

fn cached_file(path: &str) -> Result<File> {
    let mut file = File::open(path).map_err(show)?;
    let mut bytes = [0; 4096];
    file.read_exact(&mut bytes).map_err(show)?;
    if bytes.iter().any(|byte| *byte != 0x5a) {
        return Err("backend content differs".into());
    }
    file.seek(SeekFrom::Start(0)).map_err(show)?;
    Ok(file)
}

fn disconnected(file: &mut File, path: &str) -> Result<()> {
    let mut bytes = [0xa7; 64];
    if file.read(&mut bytes).is_ok() || bytes != [0xa7; 64] {
        return Err("dead server returned cached bytes or modified destination".into());
    }
    if file.metadata().is_ok() || File::open(path).is_ok() {
        return Err("dead server retained live metadata/namespace".into());
    }
    Ok(())
}

fn suite(startup: &mut Startup<'_>) -> Result<()> {
    let root = startup.take_root_directory().map_err(show)?;
    delayed_reply(startup, &root)?;
    for (name, owner_loss) in [("/fs-worker-stop", false), ("/fs-owner-loss", true)] {
        let mut worker = Worker::launch(startup, &root, name)?;
        let path = format!("{name}/cached");
        drop(cached_file(&path)?);
        receive(&worker.notice, b"read")?;
        // Reopening retains identity. No backend read is allowed for this hit.
        let mut file = cached_file(&path)?;
        let mut unexpected = [0; 16];
        if !matches!(
            worker.notice.as_byte_channel().try_receive(&mut unexpected),
            Err(hyper_os::Error::Status(hyper_os::Status::WOULD_BLOCK))
        ) {
            return Err("close/reopen lost page-cache identity".into());
        }
        if owner_loss {
            drop(worker.owner.take());
            worker.wait_retired()?;
            let reason = worker
                .process
                .as_process_supervisor()
                .info()
                .map_err(show)?
                .terminal;
            if reason != Some(ProcessTermination::ProcessExited { status: 1 }) {
                return Err(format!("owner loss did not fail the worker: {reason:?}"));
            }
        } else {
            worker.stop()?;
        }
        disconnected(&mut file, &path)?;
        println!("FS-FAILURE: {name} PASS");
    }
    let worker = Worker::launch(startup, &root, "/fs-pending-stop")?;
    std::thread::scope(|scope| -> Result<()> {
        let pending = scope.spawn(|| -> Result<()> {
            let mut file = File::open("/fs-pending-stop/blocked").map_err(show)?;
            let mut bytes = [0xa7; 32];
            if file.read(&mut bytes).is_ok() || bytes != [0xa7; 32] {
                return Err("cancelled in-flight read returned data".into());
            }
            Ok(())
        });
        receive(&worker.notice, b"blocked")?;
        worker.stop()?;
        pending.join().map_err(|_| "read thread panicked")??;
        Ok(())
    })?;
    println!("FS-FAILURE: pending-stop PASS");
    caller_stop(startup, &root)?;
    write_overflow(startup, &root)?;
    protected_file_cache(startup, &root)?;
    protection_exit(startup, &root)?;
    directory::run(startup, &root)?;
    Ok(())
}

/// Content protection blocks existing writable aliases without retaining a
/// full-file copy. Reopening still shares the ordinary coherent page cache.
fn protected_file_cache(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    let worker = Worker::launch(startup, root, "/fs-protection")?;
    let path = "/fs-protection/snapshot";
    let rights = FileRights::READ
        .union(FileRights::WRITE)
        .union(FileRights::PROTECT);
    let file = root.open(path, rights).map_err(show)?;
    let alias = root.open(path, rights).map_err(show)?;
    let ordinary = root.open(path, FileRights::READ).map_err(show)?;
    if !matches!(
        ordinary.protect(),
        Err(hyper_os::Error::Status(hyper_os::Status::ACCESS_DENIED))
    ) {
        return Err("READ implicitly granted PROTECT".into());
    }
    let protect_only = root.open(path, FileRights::PROTECT).map_err(show)?;
    let protection = protect_only.protect().map_err(show)?;
    let mut inaccessible = [0; 1];
    if !matches!(
        protect_only.read_at(0, &mut inaccessible),
        Err(hyper_os::Error::Status(hyper_os::Status::ACCESS_DENIED))
    ) {
        return Err("PROTECT implicitly granted READ".into());
    }
    drop(protect_only);
    drop(ordinary);
    if protection.byte_size() != 8192 {
        return Err("protection did not sample the current size".into());
    }
    // Admission retains complete pages. A short prefix alone deliberately
    // bypasses the cache and would leave later demand-read notices queued.
    let mut page = [0; 4096];
    file.read_exact_at(0, &mut page).map_err(show)?;
    receive(&worker.notice, b"read")?;
    if page != [0x5a; 4096] {
        return Err("protected read returned unexpected bytes".into());
    }
    let mut bytes = [0; 7];
    if !matches!(
        alias.write_at(0, b"changed"),
        Err(hyper_os::Error::Status(hyper_os::Status::BUSY))
    ) || !matches!(
        alias.resize(3),
        Err(hyper_os::Error::Status(hyper_os::Status::BUSY))
    ) {
        return Err("writable alias bypassed active content protection".into());
    }
    let second = alias.protect().map_err(show)?;
    drop(protection);
    if file.write_at(0, b"changed").is_ok() {
        return Err("closing one guard released another owner's protection".into());
    }
    drop(second);
    let guard = file.protect().map_err(show)?;
    let root_vmar = startup.borrow(startup::ROOT_VMAR).map_err(show)?;
    // SAFETY: this fixture owns this unused application range until try_close.
    let mut mapped = unsafe {
        guard.map_private_at(
            &file,
            root_vmar,
            0x1400_0000,
            PrivateMappingOptions {
                source_offset: 0,
                source_length: 4096,
                data_offset: 0,
                size: 4096,
                writable: true,
                mode: PrivateMappingMode::CopyOnWrite,
            },
        )
    }
    .map_err(show)?;
    drop(guard);
    if alias.resize(3).is_ok() {
        return Err("closing parser token released mapped-file protection".into());
    }
    // First destination access occurs through syscall copyout; it must fault
    // in and COW the mapping rather than write the protected source page.
    file.read_exact_at(0, &mut mapped.as_mut_slice().map_err(show)?[..7])
        .map_err(show)?;
    mapped.as_mut_slice().map_err(show)?[0] = 0xa5;
    file.read_exact_at(0, &mut bytes).map_err(show)?;
    if bytes != [0x5a; 7] || mapped.as_slice()[0] != 0xa5 {
        return Err("private file mapping changed protected file contents".into());
    }
    mapped.try_close().map_err(show)?;
    drop(alias);
    drop(file);
    let file = root.open(path, rights).map_err(show)?;
    file.read_exact_at(0, &mut bytes).map_err(show)?;
    let mut unexpected = [0; 16];
    if !matches!(
        worker.notice.as_byte_channel().try_receive(&mut unexpected),
        Err(hyper_os::Error::Status(hyper_os::Status::WOULD_BLOCK))
    ) {
        return Err("protected close/reopen bypassed the ordinary page cache".into());
    }
    file.write_at(0, b"changed").map_err(show)?;
    let protection = file.protect().map_err(show)?;
    file.read_exact_at(0, &mut bytes).map_err(show)?;
    receive(&worker.notice, b"read")?;
    if &bytes != b"changed" {
        return Err("write failed to invalidate cached bytes".into());
    }
    drop(protection);
    file.resize(3).map_err(show)?;
    let protection = file.protect().map_err(show)?;
    if protection.byte_size() != 3 {
        return Err("new protection retained the old size".into());
    }
    drop(protection);
    worker.stop()?;
    if file.protect().is_ok() || file.read_at(0, &mut bytes).is_ok() {
        return Err("content protection hid a terminated filesystem worker".into());
    }
    println!("FS-FAILURE: protected-file-cache PASS");
    Ok(())
}

/// Killing a process releases leaked protections before final retirement,
/// even while the parent keeps the child's address space alive with a VMAR.
fn protection_exit(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    let file = root
        .create_file(
            "/protection-exit",
            FileRights::READ
                .union(FileRights::WRITE)
                .union(FileRights::PROTECT)
                .union(FileRights::DUPLICATE)
                .union(FileRights::TRANSFER),
            0o600,
        )
        .map_err(context("create exit-test file"))?;
    file.write_at(0, &[0x5a; 4096])
        .map_err(context("write exit-test file"))?;
    let image = root
        .open("/init", hyper_service::process::EXECUTABLE_FILE_RIGHTS)
        .map_err(context("open child image"))?;
    let environment = hyper_service::process::open_environment(root, "/")
        .map_err(context("open child environment"))?;
    let loader = hyper_service::process::open_loader(
        &environment,
        hyper_service::process::LoaderKind::Bootstrap,
    )
    .map_err(context("open child loader"))?;
    let builder = ProcessBuilder::create(
        startup
            .borrow(startup::TASK_FACTORY)
            .map_err(context("borrow task factory"))?,
        startup
            .borrow(startup::TASK_GROUP)
            .map_err(context("borrow task group"))?,
        startup
            .borrow(startup::RESOURCE_DOMAIN)
            .map_err(context("borrow resource domain"))?,
        image.as_handle_ref(),
        loader.as_handle_ref(),
    )
    .map_err(context("create protector builder"))?;
    builder
        .set_name("fs-protector")
        .map_err(context("name protector"))?;
    builder
        .add_argument("/init")
        .map_err(context("add protector argv0"))?;
    builder
        .add_argument("--protector")
        .map_err(context("add protector argument"))?;
    builder
        .add_handle_duplicate(
            hyper_rt::process::console()
                .map_err(context("borrow protector console"))?
                .as_handle_ref(),
            startup::CONSOLE.as_raw(),
            RightsOffer::SameRights,
        )
        .map_err(context("delegate protector console"))?;
    builder
        .add_handle_duplicate(
            file.as_handle_ref(),
            PROTECTED_FILE.as_raw(),
            RightsOffer::Exact(Rights::READ.union(Rights::PROTECT)),
        )
        .map_err(context("delegate protected file"))?;
    let (notice, child_notice) =
        hyper_os::channel::create_pair().map_err(context("create protector notice"))?;
    builder
        .add_handle_move(
            child_notice,
            NOTICE.as_raw(),
            RightsOffer::Exact(Rights::READ.union(Rights::WRITE).union(Rights::WAIT)),
        )
        .map_err(|error| format!("delegate protector startup handle: {:?}", error.error()))?;
    let (export, child_export) =
        CapabilityChannel::create().map_err(context("create VMAR export channel"))?;
    builder
        .add_handle_move(
            child_export.into_handle(),
            EXPORTED_VMAR.as_raw(),
            RightsOffer::Exact(Rights::READ.union(Rights::WRITE).union(Rights::WAIT)),
        )
        .map_err(|error| format!("delegate protector startup handle: {:?}", error.error()))?;
    builder.seal().map_err(context("seal protector"))?;
    let child = builder
        .start()
        .map_err(|error| format!("start protector: {:?}", error.error()))?;
    let mut bytes = [MaybeUninit::uninit(); 8];
    let mut slots = [CapabilityReceiveSlot::new::<VmarObject>(Rights::INSPECT)];
    let message = export
        .receive(deadline()?, &mut bytes, &mut slots)
        .map_err(context("receive escaped VMAR"))?;
    if message.bytes() != b"vmar" || message.capability_count() != 1 {
        return Err("invalid protected child VMAR export".into());
    }
    let escaped_vmar = slots[0]
        .take::<VmarObject>()
        .map_err(context("adopt escaped VMAR"))?
        .ok_or("missing protected child VMAR")?;
    receive(&notice, b"protected")?;
    let excluded = matches!(
        file.write_at(0, b"changed"),
        Err(hyper_os::Error::Status(hyper_os::Status::BUSY))
    ) && matches!(
        file.resize(3),
        Err(hyper_os::Error::Status(hyper_os::Status::BUSY))
    );
    let supervisor = child.as_process_supervisor();
    supervisor
        .request_stop()
        .map_err(context("stop protector"))?;
    let limit = deadline()?;
    supervisor
        .wait_terminated(limit)
        .map_err(context("wait protector exit"))?;
    loop {
        match file.write_at(0, b"changed") {
            Ok(_) => break,
            Err(hyper_os::Error::Status(hyper_os::Status::BUSY)) => {}
            Err(error) => return Err(format!("write after protector exit: {error:?}")),
        }
        if hyper_os::time::monotonic_now()
            .map_err(context("check protection release deadline"))?
            .as_nanoseconds()
            >= limit
        {
            return Err("escaped VMAR retained the dead child's protection".into());
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    file.resize(3)
        .map_err(context("resize after protector exit"))?;
    if supervisor
        .info()
        .map_err(context("inspect exited protector"))?
        .phase
        == ProcessPhase::Retired
    {
        return Err("escaped VMAR did not retain the child address space".into());
    }
    drop(escaped_vmar);
    while supervisor
        .info()
        .map_err(context("inspect retired protector"))?
        .phase
        != ProcessPhase::Retired
    {
        if hyper_os::time::monotonic_now()
            .map_err(context("check retirement deadline"))?
            .as_nanoseconds()
            >= limit
        {
            return Err("protected child resource retirement timed out".into());
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    if !excluded {
        return Err("child protection did not exclude parent writes".into());
    }
    println!("FS-FAILURE: protection-exit PASS");
    Ok(())
}

fn hold_protection(startup: &mut Startup<'_>) -> Result<()> {
    let file = hyper_os::fs::File::from_handle(
        startup
            .take(PROTECTED_FILE)
            .map_err(context("take protected file"))?,
    );
    let guard = file.protect().map_err(context("protect child file"))?;
    // SAFETY: this fixture exclusively owns this unused application range.
    let mapped = unsafe {
        guard.map_private_at(
            &file,
            startup
                .borrow(startup::ROOT_VMAR)
                .map_err(context("borrow child root VMAR"))?,
            0x1400_0000,
            PrivateMappingOptions {
                source_offset: 0,
                source_length: 4096,
                data_offset: 0,
                size: 4096,
                writable: false,
                mode: PrivateMappingMode::CopyOnWrite,
            },
        )
    }
    .map_err(context("map child protected file"))?;
    if mapped.as_slice()[0] != 0x5a {
        return Err("child file mapping differs".into());
    }
    core::mem::forget(guard);
    core::mem::forget(mapped);
    let export = CapabilityChannel::from_handle(
        startup
            .take(EXPORTED_VMAR)
            .map_err(context("take VMAR export channel"))?,
    );
    let root_vmar = startup
        .borrow(startup::ROOT_VMAR)
        .map_err(context("borrow exported VMAR"))?;
    let mut offers =
        [
            CapabilityDisposition::duplicate(root_vmar, RightsOffer::Exact(Rights::INSPECT))
                .map_err(context("prepare VMAR export"))?,
        ];
    let limit = deadline()?;
    loop {
        match export.try_send(b"vmar", &mut offers) {
            Ok(()) => break,
            Err(hyper_os::Error::Status(hyper_os::Status::WOULD_BLOCK)) => {}
            Err(error) => return Err(format!("export child VMAR: {error:?}")),
        }
        wait::wait_many(
            &[WaitItem::new(
                export.as_handle_ref(),
                ObjectSignals::<CapabilityChannelObject>::PEER_RECEIVING
                    .union(ObjectSignals::<CapabilityChannelObject>::PEER_CLOSED),
            )],
            limit,
        )
        .map_err(context("wait VMAR export receiver"))?;
    }
    let notice = startup
        .take(NOTICE)
        .map_err(context("take protector notice"))?;
    notice
        .as_byte_channel()
        .send(b"protected")
        .map_err(context("send protector ready"))?;
    loop {
        std::thread::sleep(Duration::from_secs(60));
    }
}

/// A responsive worker must survive blocking RPC at non-gigahertz counter rates.
fn delayed_reply(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    // At 54 MHz a tick value mistakenly used as an absolute nanosecond
    // deadline is already in the past after about 3.43 seconds. Cross that
    // boundary even on a fast local boot; CI also selects the Pi 5 frequency.
    let now = hyper_os::time::monotonic_now()
        .map_err(show)?
        .as_nanoseconds();
    std::thread::sleep(Duration::from_nanos(5_000_000_000_u64.saturating_sub(now)));
    let worker = Worker::launch(startup, root, "/fs-delayed-reply")?;
    std::thread::scope(|scope| -> Result<()> {
        let pending = scope.spawn(|| cached_file("/fs-delayed-reply/delayed").map(drop));
        receive(&worker.notice, b"delayed")?;
        // Keep the reply withheld while the requesting thread enters wait.
        std::thread::sleep(Duration::from_millis(100));
        let returned_early = pending.is_finished();
        worker
            .notice
            .as_byte_channel()
            .send(b"release")
            .map_err(show)?;
        pending.join().map_err(|_| "read thread panicked")??;
        if returned_early {
            return Err("filesystem read returned before the worker replied".into());
        }
        Ok(())
    })?;
    drop(cached_file("/fs-delayed-reply/cached")?);
    receive(&worker.notice, b"read")?;
    worker.stop()?;
    println!("FS-FAILURE: delayed-reply PASS");
    Ok(())
}

/// A worker-controlled file size cannot wrap append or positioned-write offsets,
/// and a rejected request must leave the shared mount usable.
fn write_overflow(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    let worker = Worker::launch(startup, root, "/fs-write-overflow")?;
    let file = root
        .open("/fs-write-overflow/max-size", FileRights::WRITE)
        .map_err(show)?;
    for (name, result) in [
        ("positioned", file.write_at(u64::MAX, b"x")),
        ("append", file.append(b"x").map(|(count, _)| count)),
    ] {
        if !matches!(
            result,
            Err(hyper_os::Error::Status(hyper_os::Status::INVALID_ARGUMENT))
        ) {
            return Err(format!(
                "overflowing {name} write was not rejected: {result:?}"
            ));
        }
    }
    let mut unexpected = [0; 16];
    if !matches!(
        worker.notice.as_byte_channel().try_receive(&mut unexpected),
        Err(hyper_os::Error::Status(hyper_os::Status::WOULD_BLOCK))
    ) {
        return Err("overflowing write reached the filesystem worker".into());
    }
    drop(cached_file("/fs-write-overflow/cached")?);
    receive(&worker.notice, b"read")?;
    worker.stop()?;
    println!("FS-FAILURE: write-overflow PASS");
    Ok(())
}

/// Once a request is published, stopping its caller must drain that transaction
/// without poisoning a healthy filesystem used by unrelated processes.
fn caller_stop(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    let worker = Worker::launch(startup, root, "/fs-caller-stop")?;
    let image = root
        .open("/init", hyper_service::process::EXECUTABLE_FILE_RIGHTS)
        .map_err(show)?;
    let environment = hyper_service::process::open_environment(root, "/").map_err(show)?;
    let loader = hyper_service::process::open_loader(
        &environment,
        hyper_service::process::LoaderKind::Bootstrap,
    )
    .map_err(show)?;
    let builder = ProcessBuilder::create(
        startup.borrow(startup::TASK_FACTORY).map_err(show)?,
        startup.borrow(startup::TASK_GROUP).map_err(show)?,
        startup.borrow(startup::RESOURCE_DOMAIN).map_err(show)?,
        image.as_handle_ref(),
        loader.as_handle_ref(),
    )
    .map_err(show)?;
    builder.set_name("fs-failure-reader").map_err(show)?;
    builder.add_argument("/init").map_err(show)?;
    builder.add_argument("--reader").map_err(show)?;
    let directory = root
        .open_directory(
            "/fs-caller-stop",
            DirectoryRights::READ
                .union(DirectoryRights::INSPECT)
                .union(DirectoryRights::DUPLICATE)
                .union(DirectoryRights::EXECUTE)
                .union(DirectoryRights::PROTECT)
                .union(DirectoryRights::TRANSFER),
        )
        .map_err(show)?;
    builder
        .add_handle_move(
            directory.into_handle(),
            startup::ROOT_DIRECTORY.as_raw(),
            RightsOffer::Exact(
                Rights::READ
                    .union(Rights::EXECUTE)
                    .union(Rights::PROTECT)
                    .union(Rights::INSPECT)
                    .union(Rights::DUPLICATE),
            ),
        )
        .map_err(|error| show(error.error()))?;
    builder
        .add_handle_duplicate(
            hyper_rt::process::console().map_err(show)?.as_handle_ref(),
            startup::CONSOLE.as_raw(),
            RightsOffer::SameRights,
        )
        .map_err(show)?;
    builder.seal().map_err(show)?;
    let caller = builder.start().map_err(|error| show(error.error()))?;
    receive(&worker.notice, b"delayed")?;
    caller
        .as_process_supervisor()
        .request_stop()
        .map_err(show)?;
    // Let the woken kernel continuation observe cancellation before replying.
    // It should remain alive solely to retire the published transaction.
    std::thread::sleep(Duration::from_millis(100));
    worker
        .notice
        .as_byte_channel()
        .send(b"release")
        .map_err(show)?;
    caller
        .as_process_supervisor()
        .wait_terminated(deadline()?)
        .map_err(show)?;
    drop(
        cached_file("/fs-caller-stop/cached")
            .map_err(|error| format!("healthy mount failed after caller stop: {error}"))?,
    );
    receive(&worker.notice, b"read")?;
    worker.stop()?;
    println!("FS-FAILURE: caller-stop PASS");
    Ok(())
}

fn run() -> Result<bool> {
    let mut startup = hyper_rt::process::startup().map_err(show)?;
    if std::env::args().nth(1).as_deref() == Some("--worker") {
        let notice = startup.take(NOTICE).map_err(show)?;
        let mut volume = volume::FixtureVolume {
            notice,
            snapshot: Some(vec![0x5a; 8192]),
            directories: directory_volume::DirectoryVolume::default(),
        };
        let _completion = hyper_fs_service::serve_buffered(&mut startup, &mut volume)?;
        Ok(false)
    } else if std::env::args().nth(1).as_deref() == Some("--protector") {
        hold_protection(&mut startup)?;
        Ok(false)
    } else if std::env::args().nth(1).as_deref() == Some("--reader") {
        let mut file = File::open("/delayed").map_err(show)?;
        let mut bytes = [0; 32];
        file.read_exact(&mut bytes).map_err(show)?;
        Err("cancelled caller returned to userspace".into())
    } else {
        suite(&mut startup)?;
        Ok(true)
    }
}

fn main() -> std::process::ExitCode {
    let worker = std::env::args().nth(1).as_deref() == Some("--worker");
    match run() {
        Ok(true) => {
            println!("FS-FAILURE: PASS");
            std::process::ExitCode::SUCCESS
        }
        Ok(false) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            if worker {
                // The owner-loss case deliberately makes the production
                // server exit with an error. The parent asserts that terminal
                // status and stale-cache rejection before publishing PASS.
                eprintln!("FS-FAILURE-WORKER: {error}");
            } else {
                eprintln!("FS-FAILURE: FAIL {error}");
            }
            std::process::ExitCode::FAILURE
        }
    }
}
