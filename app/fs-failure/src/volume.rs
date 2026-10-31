// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Deterministic media substitute, exercising the production IPC server.

use hyper_filesystem::{
    Timestamp,
    protocol::{self, Entry, Error},
};
use hyper_os::handle::{ByteChannelObject, OwnedHandle};

pub(super) struct FixtureVolume {
    pub(super) notice: OwnedHandle<ByteChannelObject>,
    pub(super) snapshot: Option<Vec<u8>>,
    pub(super) directories: super::directory_volume::DirectoryVolume,
}

impl hyper_fs_service::Volume for FixtureVolume {
    fn stat(&mut self, path: &str) -> Result<Entry, Error> {
        if super::directory_volume::DirectoryVolume::owns(path) {
            return self.directories.stat(path);
        }
        let mut entry = Entry::empty();
        match path {
            "" => {}
            "cached" | "blocked" | "delayed" | "max-size" | "snapshot" => {
                entry.name[..path.len()].copy_from_slice(path.as_bytes());
                entry.name_len = path.len();
                entry.kind = protocol::FILE;
                entry.size = match path {
                    "max-size" => u64::MAX,
                    "snapshot" => self.snapshot.as_ref().ok_or(Error::Missing)?.len() as u64,
                    _ => 8192,
                };
            }
            _ => return Err(Error::Missing),
        }
        Ok(entry)
    }

    fn read(&mut self, path: &str, offset: u64, output: &mut [u8]) -> Result<usize, Error> {
        let size = self.stat(path)?.size;
        if path == "blocked" {
            self.notice
                .as_byte_channel()
                .send(b"blocked")
                .map_err(|_| Error::Closed)?;
            // The supervisor kills the actual process in this bounded wait.
            std::thread::sleep(std::time::Duration::from_secs(60));
            return Err(Error::Io);
        }
        if path == "delayed" {
            self.notice
                .as_byte_channel()
                .send(b"delayed")
                .map_err(|_| Error::Closed)?;
            super::receive(&self.notice, b"release").map_err(|_| Error::Closed)?;
        } else {
            self.notice
                .as_byte_channel()
                .send(b"read")
                .map_err(|_| Error::Closed)?;
        }
        let length = output.len().min(size.saturating_sub(offset) as usize);
        if path == "snapshot" {
            let bytes = self.snapshot.as_ref().ok_or(Error::Missing)?;
            let offset = (offset.min(size)) as usize;
            output[..length].copy_from_slice(&bytes[offset..offset + length]);
        } else {
            output[..length].fill(0x5a);
        }
        Ok(length)
    }

    fn read_directory(
        &mut self,
        path: &str,
        cursor: protocol::DirectoryCursor,
        output: &mut [u8],
    ) -> Result<usize, Error> {
        self.directories.read_directory(path, cursor, output)
    }

    fn write(&mut self, path: &str, offset: u64, input: &[u8]) -> Result<usize, Error> {
        if super::directory_volume::DirectoryVolume::owns(path) {
            return self.directories.write(path, offset, input);
        }
        if path == "snapshot" {
            let bytes = self.snapshot.as_mut().ok_or(Error::Missing)?;
            let offset = usize::try_from(offset).map_err(|_| Error::InvalidInput)?;
            let end = offset
                .checked_add(input.len())
                .filter(|end| *end <= 8192)
                .ok_or(Error::InvalidInput)?;
            bytes.resize(bytes.len().max(end), 0);
            bytes[offset..end].copy_from_slice(input);
            return Ok(input.len());
        }
        self.notice
            .as_byte_channel()
            .send(b"write")
            .map_err(|_| Error::Closed)?;
        Err(Error::Unsupported)
    }

    fn create(&mut self, path: &str, directory: bool) -> Result<(), Error> {
        if super::directory_volume::DirectoryVolume::owns(path) {
            return self.directories.create(path, directory);
        }
        if path == "snapshot" && !directory && self.snapshot.is_none() {
            self.snapshot = Some(Vec::new());
            return Ok(());
        }
        Err(Error::Unsupported)
    }

    fn remove(&mut self, path: &str) -> Result<(), Error> {
        if path == "snapshot" {
            self.snapshot.take().ok_or(Error::Missing)?;
            return Ok(());
        }
        Err(Error::Unsupported)
    }

    fn rename(&mut self, _: &str, _: &str) -> Result<(), Error> {
        Err(Error::Unsupported)
    }

    fn resize(&mut self, path: &str, length: u64) -> Result<(), Error> {
        if path == "snapshot" && length <= 8192 {
            self.snapshot
                .as_mut()
                .ok_or(Error::Missing)?
                .resize(length as usize, 0);
            return Ok(());
        }
        Err(Error::Unsupported)
    }

    fn sync(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn set_times(
        &mut self,
        _: &str,
        _: Option<Timestamp>,
        _: Option<Timestamp>,
    ) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
}
