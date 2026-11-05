// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Observe which VFS dirty pages cross the real filesystem RPC boundary.
use super::*;
use hyper_filesystem::protocol::{self, Entry, Error, SyncMode};
use hyper_fs_service::{Volume, WritebackPolicy};
use std::io::Write;

pub(super) struct ScopedVolume {
    pub(super) notice: OwnedHandle<ByteChannelObject>,
    pub(super) writes: [usize; 2],
    pub(super) synced: bool,
}
fn index(path: &str) -> std::result::Result<usize, Error> {
    match path {
        "a" => Ok(0),
        "b" => Ok(1),
        _ => Err(Error::Missing),
    }
}
impl Volume for ScopedVolume {
    fn writeback_policy(&self) -> WritebackPolicy {
        WritebackPolicy::Periodic(Duration::from_secs(3600))
    }
    fn enable_vfs_writeback(&mut self) -> std::result::Result<(), Error> {
        Ok(())
    }
    fn stat(&mut self, path: &str) -> std::result::Result<Entry, Error> {
        let mut entry = Entry::empty();
        if !path.is_empty() {
            index(path)?;
            entry.kind = protocol::FILE;
            entry.size = 3 * 4096;
            entry.name[0] = path.as_bytes()[0];
            entry.name_len = 1;
        }
        Ok(entry)
    }
    fn read(
        &mut self,
        path: &str,
        offset: u64,
        output: &mut [u8],
    ) -> std::result::Result<usize, Error> {
        index(path)?;
        let count = output
            .len()
            .min((3 * 4096u64).saturating_sub(offset) as usize);
        output[..count].fill(0);
        Ok(count)
    }
    fn write(
        &mut self,
        path: &str,
        offset: u64,
        input: &[u8],
    ) -> std::result::Result<usize, Error> {
        let index = index(path)?;
        if !matches!(offset, 0 | 8192)
            || input.len() != 4096
            || input.iter().any(|b| *b != index as u8 + 1)
        {
            return Err(Error::Corrupt);
        }
        self.writes[index] += 1;
        Ok(input.len())
    }
    fn sync_file(&mut self, path: &str, mode: SyncMode) -> std::result::Result<(), Error> {
        let expected = if !self.synced {
            ("b", SyncMode::Data, [0, 2])
        } else {
            ("a", SyncMode::All, [2, 2])
        };
        if (path, mode, self.writes) != expected {
            return Err(Error::Corrupt);
        }
        self.synced = true;
        self.notice
            .as_byte_channel()
            .send(path.as_bytes())
            .map_err(|_| Error::Closed)
    }
    fn sync(&mut self) -> std::result::Result<(), Error> {
        if self.writes != [4, 4] {
            return Err(Error::Corrupt);
        }
        self.notice
            .as_byte_channel()
            .send(b"volume")
            .map_err(|_| Error::Closed)
    }
    fn set_times(
        &mut self,
        _: &str,
        _: Option<hyper_filesystem::Timestamp>,
        _: Option<hyper_filesystem::Timestamp>,
    ) -> std::result::Result<(), Error> {
        Ok(())
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
    let worker = Worker::launch(startup, root, "/fs-sync-scope")?;
    let open = |path| {
        std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .map_err(show)
    };
    let mut a = open("/fs-sync-scope/a")?;
    let mut b = open("/fs-sync-scope/b")?;
    for (file, value) in [(&mut a, 1), (&mut b, 2)] {
        for offset in [0, 8192] {
            file.seek(SeekFrom::Start(offset)).map_err(show)?;
            file.write_all(&[value; 4096]).map_err(show)?;
        }
    }
    b.sync_data().map_err(show)?;
    receive(&worker.notice, b"b")?;
    // A is still dirty, and reads must observe it without a backend read.
    a.seek(SeekFrom::Start(8192)).map_err(show)?;
    let mut bytes = [0; 4096];
    a.read_exact(&mut bytes).map_err(show)?;
    if bytes != [1; 4096] {
        return Err("file sync lost another file's dirty cache".into());
    }
    a.sync_all().map_err(show)?;
    receive(&worker.notice, b"a")?;
    // A volume fence through B must also drain newly dirty pages belonging to A.
    for (file, value) in [(&mut a, 1), (&mut b, 2)] {
        for offset in [0, 8192] {
            file.seek(SeekFrom::Start(offset)).map_err(show)?;
            file.write_all(&[value; 4096]).map_err(show)?;
        }
    }
    let volume = root
        .open("/fs-sync-scope/b", FileRights::WRITE)
        .map_err(show)?;
    volume.sync(hyper_os::fs::SyncScope::Volume).map_err(show)?;
    receive(&worker.notice, b"volume")?;
    drop(volume);
    drop((a, b));
    worker.stop()?;
    println!("FS-FAILURE: sync-scope PASS");
    Ok(())
}
