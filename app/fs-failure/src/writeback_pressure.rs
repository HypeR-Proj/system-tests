// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! A real RPC worker observes proactive VFS drain below the hard dirty limit.

use super::*;
use hyper_filesystem::protocol::{self, Entry, Error};
use hyper_fs_service::{Volume, WritebackPolicy};
use hyper_os::fs::SyncScope;
use std::io::Write;

const PAGE: usize = 4096;
const BYTES: usize = 7 * 1024 * 1024;
const PAGES: usize = BYTES / PAGE;
const BUFFER: usize = 64 * 1024;
const BATCH: usize = 512 * 1024;

fn pattern(page: usize) -> u8 {
    (page % 251 + 1) as u8
}

fn path_check(path: &str) -> std::result::Result<(), Error> {
    if path == "payload" {
        Ok(())
    } else {
        Err(Error::Missing)
    }
}

pub(super) struct PressureVolume {
    notice: OwnedHandle<ByteChannelObject>,
    written: Vec<bool>,
    notified: bool,
}

impl PressureVolume {
    pub(super) fn new(notice: OwnedHandle<ByteChannelObject>) -> Self {
        Self {
            notice,
            // The fake backend stores only page acceptance, not another 7 MiB cache.
            written: vec![false; PAGES],
            notified: false,
        }
    }
}

impl Volume for PressureVolume {
    fn writeback_policy(&self) -> WritebackPolicy {
        WritebackPolicy::Periodic(Duration::from_secs(3600))
    }

    fn enable_vfs_writeback(&mut self) -> std::result::Result<(), Error> {
        Ok(())
    }

    fn stat(&mut self, path: &str) -> std::result::Result<Entry, Error> {
        let mut entry = Entry::empty();
        if !path.is_empty() {
            path_check(path)?;
            entry.kind = protocol::FILE;
            entry.size = BYTES as u64;
            entry.name[..7].copy_from_slice(b"payload");
            entry.name_len = 7;
        }
        Ok(entry)
    }

    fn read(
        &mut self,
        path: &str,
        offset: u64,
        output: &mut [u8],
    ) -> std::result::Result<usize, Error> {
        path_check(path)?;
        let count = output
            .len()
            .min((BYTES as u64).saturating_sub(offset) as usize);
        for (index, byte) in output[..count].iter_mut().enumerate() {
            let page = (offset as usize + index) / PAGE;
            *byte = if self.written[page] { pattern(page) } else { 0 };
        }
        Ok(count)
    }

    fn write(
        &mut self,
        path: &str,
        offset: u64,
        input: &[u8],
    ) -> std::result::Result<usize, Error> {
        path_check(path)?;
        let start = usize::try_from(offset).map_err(|_| Error::Corrupt)?;
        let end = start.checked_add(input.len()).ok_or(Error::Corrupt)?;
        if input.is_empty()
            || input.len() > BATCH
            || !start.is_multiple_of(PAGE)
            || !input.len().is_multiple_of(PAGE)
            || end > BYTES
        {
            return Err(Error::Corrupt);
        }
        for (index, chunk) in input.chunks_exact(PAGE).enumerate() {
            let page = start / PAGE + index;
            if self.written[page] || chunk.iter().any(|byte| *byte != pattern(page)) {
                return Err(Error::Corrupt);
            }
        }
        self.written[start / PAGE..end / PAGE].fill(true);
        if !self.notified {
            self.notice
                .as_byte_channel()
                .send(b"background")
                .map_err(|_| Error::Closed)?;
            self.notified = true;
        }
        Ok(input.len())
    }

    fn sync(&mut self) -> std::result::Result<(), Error> {
        if self.written.iter().any(|written| !written) {
            return Err(Error::Corrupt);
        }
        self.notice
            .as_byte_channel()
            .send(b"synced")
            .map_err(|_| Error::Closed)
    }

    fn sync_file(&mut self, _: &str, _: protocol::SyncMode) -> std::result::Result<(), Error> {
        // This test's explicit fence must select the volume operation.
        Err(Error::Corrupt)
    }

    fn set_times(
        &mut self,
        path: &str,
        _: Option<hyper_filesystem::Timestamp>,
        _: Option<hyper_filesystem::Timestamp>,
    ) -> std::result::Result<(), Error> {
        path_check(path)
    }

    fn read_directory(
        &mut self,
        _: &str,
        _: protocol::DirectoryCursor,
        _: &mut [u8],
    ) -> std::result::Result<usize, Error> {
        Err(Error::Unsupported)
    }
    fn create(&mut self, _: &str, _: bool) -> std::result::Result<(), Error> {
        Err(Error::Unsupported)
    }
    fn remove(&mut self, _: &str) -> std::result::Result<(), Error> {
        Err(Error::Unsupported)
    }
    fn rename(&mut self, _: &str, _: &str) -> std::result::Result<(), Error> {
        Err(Error::Unsupported)
    }
    fn resize(&mut self, _: &str, _: u64) -> std::result::Result<(), Error> {
        Err(Error::Unsupported)
    }
}

pub(super) fn run(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    let worker = Worker::launch(startup, root, "/fs-writeback-pressure")?;
    let path = "/fs-writeback-pressure/payload";
    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .map_err(show)?;
    let mut bytes = vec![0; BUFFER];
    for offset in (0..BYTES).step_by(BUFFER) {
        for (index, chunk) in bytes.chunks_exact_mut(PAGE).enumerate() {
            chunk.fill(pattern(offset / PAGE + index));
        }
        file.write_all(&bytes).map_err(show)?;
    }
    // Seven MiB is below the eight MiB admission limit. No explicit sync or
    // one-hour periodic deadline can explain this writeback. Wait on a notice,
    // not a guessed worker delay, before invoking any synchronization API.
    receive(&worker.notice, b"background")?;
    file.seek(SeekFrom::Start(0)).map_err(show)?;
    for offset in (0..BYTES).step_by(BUFFER) {
        file.read_exact(&mut bytes).map_err(show)?;
        for (index, chunk) in bytes.chunks_exact(PAGE).enumerate() {
            if chunk
                .iter()
                .any(|byte| *byte != pattern(offset / PAGE + index))
            {
                return Err("partial writeback lost a dirty or completed page".into());
            }
        }
    }
    let fence = root.open(path, FileRights::WRITE).map_err(show)?;
    fence.sync(SyncScope::Volume).map_err(show)?;
    receive(&worker.notice, b"synced")?;
    drop((file, fence));
    worker.stop()?;
    println!("FS-FAILURE: writeback-pressure PASS");
    Ok(())
}
