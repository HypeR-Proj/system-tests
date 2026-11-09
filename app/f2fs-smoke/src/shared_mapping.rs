// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Coherent Native shared-file mappings, including file recovery after reboot.

mod pressure;
mod process;
pub use process::child;

use hyper_os::{
    Error, Status, channel,
    fs::{Directory, File, FileRights, SyncScope},
    handle::{HandleRef, ObjectType, OwnedHandle, Rights, VmarObject},
    memory::{PAGE_SIZE, SharedFileMapping, SharedFileMappingOptions},
    startup,
};
use std::path::Path;

type Result<T = ()> = std::result::Result<T, Box<dyn std::error::Error>>;
const PAGE: usize = PAGE_SIZE as usize;
const LENGTH: usize = 3 * PAGE;
const NAME: &str = "f2fs-shared-mapping";

// These are installed libsystem C entry points, not a private trap or syscall
// number. A raw output pointer is necessary to exercise kernel copy-to-user
// dirty admission without inventing a safe mutable slice over shared memory.
#[repr(C)]
struct CallResult {
    status: i64,
    value0: u64,
    value1: u64,
}
unsafe extern "C" {
    fn hyper_file_read_at(file: u64, offset: u64, output: *mut u8, capacity: usize) -> CallResult;
    fn hyper_byte_channel_read(endpoint: u64, output: *mut u8, capacity: usize) -> CallResult;
    fn hyper_vmar_protect(vmar: u64, address: usize, size: usize, permissions: u32) -> i64;
}
const READ: u32 = 1;
const WRITE: u32 = 2;
const EXECUTE: u32 = 4;

fn options(offset: usize, length: usize, writable: bool) -> SharedFileMappingOptions {
    SharedFileMappingOptions {
        file_offset: offset as u64,
        length: length as u64,
        writable,
    }
}

fn native_open(directory: &Directory, path: &Path, rights: FileRights) -> Result<File> {
    // The startup directory is the process namespace root, not /data.
    let relative = path
        .to_str()
        .ok_or("non-UTF8 fixture path")?
        .trim_start_matches('/');
    Ok(directory.open(relative, rights)?)
}

fn expected_status<T>(result: hyper_os::Result<T>, expected: Status, operation: &str) -> Result {
    match result {
        Err(Error::Status(actual)) if actual == expected => Ok(()),
        Err(error) => Err(format!("{operation}: expected {expected:?}, got {error:?}").into()),
        Ok(_) => Err(format!("{operation}: unexpectedly succeeded").into()),
    }
}

fn put(mapping: &SharedFileMapping, offset: usize, bytes: &[u8]) -> Result {
    if offset
        .checked_add(bytes.len())
        .is_none_or(|end| end > mapping.len())
    {
        return Err("test write exceeded mapping".into());
    }
    let address = mapping.as_mut_ptr()?;
    for (index, byte) in bytes.iter().copied().enumerate() {
        // SAFETY: this single-threaded test retains the mapped owner, bounds
        // checks every offset and performs no concurrent application accesses.
        // Volatile accesses keep alias-coherence observations explicit.
        unsafe { address.add(offset + index).write_volatile(byte) };
    }
    Ok(())
}

fn check(mapping: &SharedFileMapping, offset: usize, expected: &[u8]) -> Result {
    if offset
        .checked_add(expected.len())
        .is_none_or(|end| end > mapping.len())
    {
        return Err("test read exceeded mapping".into());
    }
    for (index, byte) in expected.iter().copied().enumerate() {
        // SAFETY: live mapped range, checked bounds, and all test writes and
        // file operations complete before these sequential observations.
        if unsafe { mapping.as_ptr().add(offset + index).read_volatile() } != byte {
            return Err(format!("mapping mismatch at {}", offset + index).into());
        }
    }
    Ok(())
}

fn status(raw: i64) -> hyper_os::Result<()> {
    let value = Status::from_raw(raw);
    if value == Status::OK {
        Ok(())
    } else {
        Err(Error::Status(value))
    }
}

fn with_raw<T: ObjectType, R>(handle: OwnedHandle<T>, operation: impl FnOnce(u64) -> R) -> R {
    let raw = handle.into_raw();
    // SAFETY: into_raw just relinquished the sole Rust owner; this scope
    // immediately restores it and keeps the capability alive through operation.
    let _owner = unsafe { OwnedHandle::<T>::from_raw_owned(raw) };
    operation(raw.get())
}

fn protect(mapping: &SharedFileMapping, permissions: u32) -> hyper_os::Result<()> {
    let handle = mapping.as_handle_ref()?.duplicate(Rights::MAP)?;
    with_raw(handle, |raw| {
        // SAFETY: this test owns the child and retains it throughout the call.
        // It stops memory accesses while changing permissions and never executes
        // file bytes, including if a negative EXEC check unexpectedly succeeds.
        status(unsafe {
            hyper_vmar_protect(raw, mapping.as_ptr() as usize, mapping.len(), permissions)
        })
    })
}

fn negative_cases(
    directory: &Directory,
    root_vmar: HandleRef<'_, VmarObject>,
    path: &Path,
    file: &File,
    mapping: &SharedFileMapping,
) -> Result {
    expected_status(
        file.resize((LENGTH - PAGE) as u64),
        Status::BUSY,
        "mapped shrink",
    )?;
    expected_status(
        file.resize((LENGTH + PAGE) as u64),
        Status::BUSY,
        "mapped extend",
    )?;
    // No-op resize and within-extent writes remain usable.
    file.resize(LENGTH as u64)?;
    expected_status(
        file.write_at(LENGTH as u64, b"x"),
        Status::BUSY,
        "extending write",
    )?;
    expected_status(file.append(b"x"), Status::BUSY, "mapped append")?;
    expected_status(
        file.protect(),
        Status::BUSY,
        "protection after writable mapping",
    )?;
    expected_status(
        protect(mapping, READ | EXECUTE),
        Status::ACCESS_DENIED,
        "shared EXEC",
    )?;

    if root_vmar.info()?.rights.contains(Rights::LOCK_MEMORY) {
        expected_status(
            hyper_os::memory::lock_memory(root_vmar, mapping.as_ptr() as u64, PAGE as u64),
            Status::NOT_SUPPORTED,
            "shared residency locking",
        )?;
    }
    // A plain per-process atomic wait must not accidentally treat this shared
    // canonical page as a private/VMO wait identity. This test is single-threaded
    // here, so temporarily borrowing an aligned AtomicU32 has no mixed access.
    // SAFETY: mapping owns this aligned word; no application writer runs here.
    let word = unsafe { std::sync::atomic::AtomicU32::from_ptr(mapping.as_mut_ptr()?.cast()) };
    let expected = word.load(std::sync::atomic::Ordering::Relaxed);
    let deadline = hyper_os::time::deadline_after(std::time::Duration::from_millis(100))?.as_raw();
    expected_status(
        hyper_os::thread::atomic_wait(word, expected, deadline),
        Status::NOT_SUPPORTED,
        "shared local atomic wait",
    )?;
    expected_status(
        hyper_os::thread::atomic_wake(word, 1),
        Status::NOT_SUPPORTED,
        "shared local atomic wake",
    )?;

    let read_only = native_open(directory, path, FileRights::READ)?;
    expected_status(
        read_only.map_shared(root_vmar, options(0, PAGE, true)),
        Status::ACCESS_DENIED,
        "READ source mapped WRITE",
    )?;
    let read_mapping = read_only.map_shared(root_vmar, options(0, PAGE, false))?;
    expected_status(
        protect(&read_mapping, READ | WRITE),
        Status::ACCESS_DENIED,
        "WRITE ceiling",
    )?;
    expected_status(
        read_mapping.sync(SyncScope::Data),
        Status::ACCESS_DENIED,
        "READ-only sync authority",
    )?;
    expected_status(
        read_mapping.as_mut_ptr(),
        Status::ACCESS_DENIED,
        "read-only SDK pointer",
    )?;
    read_mapping
        .try_close()
        .map_err(|e| format!("close read mapping: {e:?}"))?;

    // Source-derived WRITE authority permits an upgrade from initial READ.
    let potential_write = file.map_shared(root_vmar, options(PAGE, PAGE, false))?;
    protect(&potential_write, READ | WRITE)?;
    protect(&potential_write, READ)?;
    potential_write
        .try_close()
        .map_err(|e| format!("close potential mapping: {e:?}"))?;
    Ok(())
}

fn isolated_potential_write(
    directory: &Directory,
    root_vmar: HandleRef<'_, VmarObject>,
    root: &Path,
) -> Result {
    let path = root.join("f2fs-shared-potential-write");
    std::fs::write(&path, vec![0u8; PAGE])?;
    let file = native_open(
        directory,
        &path,
        FileRights::READ
            .union(FileRights::WRITE)
            .union(FileRights::PROTECT),
    )?;
    // No other mapping of this file exists: BUSY must come from the retained
    // WRITE ceiling, not an unrelated currently-writable alias.
    let mapping = file.map_shared(root_vmar, options(0, PAGE, false))?;
    expected_status(
        file.protect(),
        Status::BUSY,
        "isolated potential WRITE lease",
    )?;
    mapping.sync(SyncScope::Data)?;
    mapping
        .try_close()
        .map_err(|e| format!("potential WRITE close: {e:?}"))?;
    // Successful unmap must retire that authority before acknowledging close.
    let protection = file.protect()?;
    drop(protection);
    drop(file);
    super::remove_verified_fixture(&path).map_err(|e| format!("potential WRITE cleanup: {e}"))?;
    Ok(())
}

fn concurrent_freeze(mapping: &SharedFileMapping, alias: &SharedFileMapping) -> Result {
    use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
    use std::time::{Duration, Instant};
    let stop = AtomicBool::new(false);
    let started = AtomicBool::new(false);
    let address = mapping.as_mut_ptr()? as usize + PAGE;
    // SAFETY: this byte stays in mapping until after scope joins. While the
    // writer is live all application accesses to it use AtomicU8; main does not
    // inspect aliases or ordinary file bytes until the writer has stopped.
    let word = unsafe { AtomicU8::from_ptr(address as *mut u8) };
    let rounds = std::thread::scope(|scope| -> Result<usize> {
        let worker = scope.spawn(|| {
            let mut rounds = 0usize;
            while !stop.load(Ordering::Acquire) {
                word.store((rounds & 0xff) as u8, Ordering::Relaxed);
                started.store(true, Ordering::Release);
                rounds = rounds.wrapping_add(1);
                std::thread::yield_now();
            }
            rounds
        });
        let result = (|| -> Result {
            let until = Instant::now();
            while !started.load(Ordering::Acquire) {
                if until.elapsed() > Duration::from_secs(5) {
                    return Err("mapped writer did not start".into());
                }
                std::thread::yield_now();
            }
            for _ in 0..8 {
                mapping.sync(SyncScope::Data)?;
            }
            Ok(())
        })();
        // Even an early sync error must stop/join before the owner can unmap.
        stop.store(true, Ordering::Release);
        let rounds = worker.join().map_err(|_| "mapped writer panicked")?;
        result?;
        Ok(rounds)
    })?;
    if rounds == 0 {
        return Err("mapped writer made no progress".into());
    }
    word.store(0x48, Ordering::Release);
    mapping.sync(SyncScope::Data)?;
    check(alias, 0, &[0x48])?;
    println!("F2FS-SMOKE: concurrent mapped writer/freeze PASS stores={rounds}");
    Ok(())
}

pub fn write(root: &Path) -> Result {
    let mut startup = hyper_rt::process::startup()?;
    let directory = startup.take_root_directory()?;
    let root_vmar = startup.borrow(startup::ROOT_VMAR)?;
    let path = root.join(NAME);
    isolated_potential_write(&directory, root_vmar, root)?;
    std::fs::write(&path, vec![0u8; LENGTH])?;
    let rights = FileRights::READ
        .union(FileRights::WRITE)
        .union(FileRights::PROTECT);
    let file = native_open(&directory, &path, rights)?;
    let protected_path = root.join("f2fs-shared-protected");
    std::fs::write(&protected_path, vec![0u8; PAGE])?;
    let protected_file = native_open(&directory, &protected_path, rights)?;
    let protection = protected_file.protect()?;
    expected_status(
        protected_file.map_shared(root_vmar, options(0, PAGE, true)),
        Status::BUSY,
        "mapping after content protection",
    )?;
    drop(protection);
    drop(protected_file);
    super::remove_verified_fixture(&protected_path)
        .map_err(|e| format!("protected fixture cleanup: {e}"))?;
    let mapping = file.map_shared(root_vmar, options(0, LENGTH, true))?;
    let alias = file.map_shared(root_vmar, options(PAGE, PAGE, true))?;
    expected_status(
        file.map_shared(root_vmar, options(LENGTH, PAGE, true)),
        Status::INVALID_ARGUMENT,
        "mapping beyond EOF",
    )?;
    check(&mapping, 0, &vec![0; LENGTH])?;
    put(&mapping, PAGE, &vec![0x47; PAGE])?;
    check(&alias, 0, &vec![0x47; PAGE])?;
    put(&alias, 0, &vec![0x48; PAGE])?;
    check(&mapping, PAGE, &vec![0x48; PAGE])?;
    let mut observed = vec![0; PAGE];
    file.read_exact_at(PAGE as u64, &mut observed)?;
    if observed != vec![0x48; PAGE] {
        return Err("mapped writes absent from ordinary read".into());
    }
    if file.write_at(0, &vec![0x31; PAGE])? != PAGE {
        return Err("short ordinary write".into());
    }
    check(&mapping, 0, &vec![0x31; PAGE])?;
    negative_cases(&directory, root_vmar, &path, &file, &mapping)?;
    concurrent_freeze(&mapping, &alias)?;
    process::run(root, &mapping)?;
    pressure::run(&directory, root_vmar, root)?;
    mapping.sync(SyncScope::Data)?;

    // Redirty after a successful write-protect/fence cycle. The second cycle
    // must observe both CPU stores and writes performed by copy_to_user.
    put(&mapping, 0, &[0x7a])?;
    let (sender, receiver) = channel::create_pair()?;
    sender.as_byte_channel().try_send(&[0xc3; 64])?;
    let output = mapping.as_mut_ptr()?;
    let received = with_raw(receiver, |raw| {
        // SAFETY: this second-page range is owned, writable, and application
        // aliases are idle. IPC must admit dirty backing before committing the
        // queued message and writing into a write-protected post-sync page.
        unsafe { hyper_byte_channel_read(raw, output.add(PAGE + 64), 64) }
    });
    status(received.status)?;
    if received.value0 != 64 {
        return Err("short mapped byte-channel output".into());
    }
    check(&alias, 64, &[0xc3; 64])?;
    drop(sender);
    let source_path = root.join("f2fs-shared-copy-source");
    std::fs::write(&source_path, vec![0x62u8; PAGE])?;
    let source = native_open(&directory, &source_path, FileRights::READ)?;
    // SAFETY: owned writable third page is idle and large enough; source is a
    // different file, so there is no overlapping input/output cache range.
    let output = mapping.as_mut_ptr()?;
    let copied = with_raw(source.into_handle(), |raw| {
        // SAFETY: the third page is owned, writable and unused during the call;
        // the input is a distinct file and PAGE bytes fit this mapped extent.
        unsafe { hyper_file_read_at(raw, 0, output.add(2 * PAGE), PAGE) }
    });
    status(copied.status)?;
    if copied.value0 != PAGE as u64 || copied.value1 != PAGE as u64 {
        return Err("short mapped syscall output".into());
    }
    check(&mapping, 2 * PAGE, &vec![0x62; PAGE])?;
    super::remove_verified_fixture(&source_path)
        .map_err(|e| format!("copy source cleanup: {e}"))?;
    alias
        .try_close()
        .map_err(|e| format!("close alias: {e:?}"))?;
    drop(file);
    mapping.sync(SyncScope::Full)?;
    put(&mapping, PAGE, &[0xa4])?;
    mapping.sync_range(PAGE, PAGE, SyncScope::Data)?;
    mapping
        .try_close()
        .map_err(|e| format!("close mapping: {e:?}"))?;

    // Lease retirement must restore ordinary extension/append; the persistent
    // main fixture remains unchanged for the unclean-restart assertion.
    let retired_path = root.join("f2fs-shared-retired");
    std::fs::write(&retired_path, vec![0u8; PAGE])?;
    let retired_file = native_open(&directory, &retired_path, rights)?;
    let retired_mapping = retired_file.map_shared(root_vmar, options(0, PAGE, true))?;
    put(&retired_mapping, 0, &[0x5a])?;
    retired_mapping
        .try_close()
        .map_err(|e| format!("retire extent: {e:?}"))?;
    retired_file.resize((2 * PAGE) as u64)?;
    let mut retained = [0u8; 1];
    retired_file.read_exact_at(0, &mut retained)?;
    if retained != [0x5a] {
        return Err("unmap/resize lost dirty mapped content".into());
    }
    if retired_file.append(b"tail")? != (4, (2 * PAGE + 4) as u64) {
        return Err("append remained restricted after final unmap".into());
    }
    retired_file.sync(SyncScope::Full)?;
    drop(retired_file);
    super::remove_verified_fixture(&retired_path)
        .map_err(|e| format!("retired fixture cleanup: {e}"))?;

    unsupported_at(
        &directory,
        root_vmar,
        Path::new("/tmp/f2fs-shared-unsupported"),
    )?;
    println!(
        "F2FS-SMOKE: shared alias, ordinary I/O, syscall output, rights, fixed extent and redirty PASS"
    );
    Ok(())
}

fn unsupported_at(
    directory: &Directory,
    root_vmar: HandleRef<'_, VmarObject>,
    path: &Path,
) -> Result {
    // The same admission contract applies to RAMFS and synchronous FAT; failure
    // must not leave the ordinary file stuck in a partially promoted state.
    std::fs::write(path, vec![0u8; PAGE])?;
    let file = native_open(directory, path, FileRights::READ.union(FileRights::WRITE))?;
    expected_status(
        file.map_shared(root_vmar, options(0, PAGE, true)),
        Status::NOT_SUPPORTED,
        "unsupported backend shared mapping",
    )?;
    if file.write_at(0, b"ok")? != 2 {
        return Err("rejected mapping broke ordinary write".into());
    }
    file.sync(SyncScope::Full)?;
    let mut bytes = [0; 2];
    file.read_exact_at(0, &mut bytes)?;
    if &bytes != b"ok" {
        return Err("rejected mapping broke ordinary read".into());
    }
    drop(file);
    super::remove_verified_fixture(path)
        .map_err(|e| format!("unsupported fixture cleanup: {e}"))?;
    Ok(())
}

pub fn unsupported(root: &Path) -> Result {
    let mut startup = hyper_rt::process::startup()?;
    let directory = startup.take_root_directory()?;
    let root_vmar = startup.borrow(startup::ROOT_VMAR)?;
    unsupported_at(&directory, root_vmar, &root.join("shared-unsupported"))
}

pub fn verify(root: &Path) -> Result {
    let path = root.join(NAME);
    let mut expected = vec![0x31; PAGE];
    expected.extend(vec![0x48; PAGE]);
    expected.extend(vec![0x62; PAGE]);
    expected[0] = 0x7a;
    expected[PAGE] = 0xa4;
    expected[PAGE + 64..PAGE + 128].fill(0xc3);
    if std::fs::read(&path)? != expected {
        return Err("shared-map sync did not survive unclean restart".into());
    }
    super::remove_verified_fixture(&path).map_err(|e| format!("shared fixture cleanup: {e}"))?;
    Ok(())
}
