// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Real Native enumeration validates shared batching and attribute coherence.

use super::{Result, Worker, context, show};
use hyper_os::{
    fs::{Directory, DirectoryRights},
    startup::Startup,
};
use std::io::Write;
use std::sync::Barrier;

const MOUNT: &str = "/fs-directories";

fn counters(sequence: usize) -> Result<(u32, u32)> {
    // Every sample uses a distinct path, so the measurement itself cannot be
    // answered by an older cached stat. No performance timing enters this test.
    let bits = std::fs::metadata(format!("{MOUNT}/counter-{sequence}"))
        .map_err(show)?
        .len();
    Ok(((bits >> 32) as u32, bits as u32))
}

fn scan(first_size: u64, added: bool) -> Result<()> {
    scan_entries(added, |entry, index| check_entry(entry, index, first_size))
}

fn scan_names() -> Result<()> {
    scan_entries(false, |entry, index| check_name(&entry, index).map(drop))
}

fn scan_entries(
    added: bool,
    mut check: impl FnMut(std::fs::DirEntry, usize) -> Result<()>,
) -> Result<()> {
    let mut count = 0;
    for entry in std::fs::read_dir(format!("{MOUNT}/entries")).map_err(show)? {
        check(entry.map_err(show)?, count)?;
        count += 1;
    }
    if count != super::directory_volume::CHILDREN + usize::from(added) {
        return Err(format!("directory lost children: {count}"));
    }
    Ok(())
}

fn check_name(entry: &std::fs::DirEntry, index: usize) -> Result<String> {
    let expected = if index == super::directory_volume::CHILDREN {
        "added".to_owned()
    } else {
        format!("item{index:03}")
    };
    if entry.file_name() != expected.as_str() {
        return Err(format!(
            "unexpected directory name {:?} instead of {expected}",
            entry.file_name()
        ));
    }
    Ok(expected)
}

fn check_entry(entry: std::fs::DirEntry, index: usize, first_size: u64) -> Result<()> {
    let expected = check_name(&entry, index)?;
    let size = if index == 0 {
        first_size
    } else if index == super::directory_volume::CHILDREN {
        0
    } else {
        index as u64
    };
    if entry.metadata().map_err(show)?.len() != size {
        return Err(format!("stale metadata for {expected}"));
    }
    Ok(())
}

fn verify_cache_reuse() -> Result<()> {
    scan(0, false).map_err(context("cold directory scan"))?;
    // Asynchronous cache-table growth/compaction temporarily uses direct I/O.
    // Bound the attempts to observe a fully cached reopen, while checking the
    // complete directory even when maintenance overlaps an earlier attempt.
    const ATTEMPTS: usize = 16;
    let mut names_delta = (0, 0);
    let mut metadata_delta = None;
    for attempt in 0..ATTEMPTS {
        // Namespace mutation advances the mount's metadata identity. A failed
        // metadata probe must not prime the next trial through its stat RPCs;
        // only name enumeration may populate this trial's child attributes.
        std::fs::remove_file(format!("{MOUNT}/snapshot")).map_err(show)?;
        drop(std::fs::File::create(format!("{MOUNT}/snapshot")).map_err(show)?);
        scan_names().map_err(context("prime directory names"))?;
        let primed = counters(attempt * 3 + 1)?;
        scan_names().map_err(context("reopen directory names"))?;
        let reopened = counters(attempt * 3 + 2)?;
        names_delta = (reopened.0 - primed.0, reopened.1 - primed.1);
        metadata_delta = None;
        if names_delta != (0, 0) {
            continue;
        }
        scan(0, false).map_err(context("reopened directory attributes"))?;
        let checked = counters(attempt * 3 + 3)?;
        metadata_delta = Some((checked.0 - reopened.0, checked.1 - reopened.1));
        if metadata_delta == Some((0, 0)) {
            return Ok(());
        }
    }
    Err(format!(
        "directory cache did not retain enumeration/attributes after {ATTEMPTS} attempts: \
         last backend read/stat deltas names={names_delta:?}, metadata={metadata_delta:?}"
    ))
}

/// Each round races two independent source scans against namespace changes in
/// another directory. Error paths still rendezvous, so a useful failure cannot
/// strand another test thread at a barrier and hide the original diagnosis.
fn concurrent_readers(first_size: u64) -> Result<()> {
    const READERS: usize = 2;
    const PER_ROUND: usize = 4;
    const ROUNDS: usize = super::directory_volume::CHILDREN / PER_ROUND + 1;
    let barrier = Barrier::new(READERS + 1);
    std::thread::scope(|scope| -> Result<()> {
        let reader = || -> Result<()> {
            let mut stream = std::fs::read_dir(format!("{MOUNT}/entries")).map_err(show);
            let mut result = Ok(());
            let mut count = 0;
            for _ in 0..ROUNDS {
                barrier.wait();
                if result.is_ok() {
                    result = (|| {
                        let stream = stream.as_mut().map_err(|error| error.clone())?;
                        for _ in 0..PER_ROUND {
                            match stream.next() {
                                Some(entry) => {
                                    check_entry(entry.map_err(show)?, count, first_size)?;
                                    count += 1;
                                }
                                None if count == super::directory_volume::CHILDREN => break,
                                None => return Err(format!("racing reader stopped at {count}")),
                            }
                        }
                        Ok(())
                    })();
                }
                barrier.wait();
            }
            result?;
            if count != super::directory_volume::CHILDREN {
                return Err(format!("racing reader returned {count} children"));
            }
            Ok(())
        };
        let first = scope.spawn(reader);
        let second = scope.spawn(reader);
        let mut writer_result = Ok(());
        for _ in 0..ROUNDS {
            barrier.wait();
            if writer_result.is_ok() {
                writer_result = (|| {
                    drop(std::fs::File::create(format!("{MOUNT}/snapshot")).map_err(show)?);
                    std::fs::remove_file(format!("{MOUNT}/snapshot")).map_err(show)
                })();
            }
            barrier.wait();
        }
        let first_result = first
            .join()
            .map_err(|_| "first directory reader panicked")?;
        let second_result = second
            .join()
            .map_err(|_| "second directory reader panicked")?;
        writer_result?;
        first_result?;
        second_result
    })
}

fn unsupported_name_preserves_mount(worker: &Worker) -> Result<()> {
    let mut stream = std::fs::read_dir(format!("{MOUNT}/long-names")).map_err(show)?;
    match stream.next() {
        Some(Err(error)) if error.kind() == std::io::ErrorKind::Unsupported => {}
        other => return Err(format!("oversized valid backend name: {other:?}")),
    }
    // This file was not inspected or cached by the directory tests, so both
    // operations must still reach the live worker after enumeration fails.
    let path = format!("{MOUNT}/cached");
    if std::fs::metadata(&path).map_err(show)?.len() != 8192 {
        return Err("oversized directory name changed unrelated metadata".into());
    }
    drop(super::cached_file(&path)?);
    super::receive(&worker.notice, b"read")
}

pub(super) fn run(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    let worker = Worker::launch(startup, root, MOUNT)?;
    verify_cache_reuse()?;
    unsupported_name_preserves_mount(&worker)
        .map_err(context("unrepresentable directory name preserves mount"))?;

    let directory = root
        .open_directory(&format!("{MOUNT}/entries"), DirectoryRights::READ)
        .map_err(show)?;
    let mut reader = directory.reader().map_err(show)?;
    reader
        .next_page()
        .map_err(show)?
        .ok_or("empty independent directory")?;
    // Mutating a different directory must not invalidate this source scan
    // (recursive copies commonly create destination entries in the same FS).
    std::fs::remove_file(format!("{MOUNT}/snapshot")).map_err(show)?;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .open(format!("{MOUNT}/entries/item000"))
        .map_err(show)?;
    file.write_all(b"changed").map_err(show)?;
    drop(file);
    if reader.next_page().map_err(show)?.is_none() {
        return Err("data write incorrectly invalidated directory continuation".into());
    }
    scan(7, false).map_err(context("directory attributes after write"))?;
    concurrent_readers(7).map_err(context("concurrent directory readers and mutation"))?;

    let mut stale = directory.reader().map_err(show)?;
    stale
        .next_page()
        .map_err(show)?
        .ok_or("missing directory page before mutation")?;
    let mut stale_std = std::fs::read_dir(format!("{MOUNT}/entries")).map_err(show)?;
    for _ in 0..4 {
        stale_std
            .next()
            .ok_or("missing std directory entry before mutation")?
            .map_err(show)?;
    }
    drop(std::fs::File::create(format!("{MOUNT}/entries/added")).map_err(show)?);
    if stale.next_page().is_ok() {
        return Err("namespace mutation did not reject the old backend checkpoint".into());
    }
    if !matches!(stale_std.next(), Some(Err(_))) || stale_std.next().is_some() {
        return Err("std directory iterator did not stop after a stale checkpoint".into());
    }
    scan(7, true).map_err(context("directory after create"))?;
    let mut cached = directory.reader().map_err(show)?;
    cached
        .next_page()
        .map_err(show)?
        .ok_or("missing warm page before worker stop")?;
    worker.stop()?;
    if cached.next_page().is_ok() || std::fs::metadata(format!("{MOUNT}/entries/item000")).is_ok() {
        return Err("warm namespace cache hid worker death".into());
    }
    println!("FS-FAILURE: directory-cache PASS");
    Ok(())
}
