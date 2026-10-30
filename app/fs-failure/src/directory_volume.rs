// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Operation-count fixture for the real directory IPC and kernel cache path.

use hyper_filesystem::protocol::{self, DirectoryBatchBuilder, DirectoryCursor, Entry, Error};

pub(super) const CHILDREN: usize = 96;

#[derive(Default)]
pub(super) struct DirectoryVolume {
    reads: u32,
    stats: u32,
    first_size: u64,
    added: bool,
}

impl DirectoryVolume {
    pub(super) fn owns(path: &str) -> bool {
        path == "entries"
            || path == "long-names"
            || path.starts_with("entries/")
            || path.starts_with("counter-")
    }

    pub(super) fn stat(&mut self, path: &str) -> Result<Entry, Error> {
        if path.starts_with("counter-") {
            return Ok(file(
                path,
                (u64::from(self.reads) << 32) | u64::from(self.stats),
            ));
        }
        if matches!(path, "entries" | "long-names") {
            let mut entry = Entry::empty();
            entry.name[..path.len()].copy_from_slice(path.as_bytes());
            entry.name_len = path.len();
            return Ok(entry);
        }
        self.stats += 1;
        let name = path.strip_prefix("entries/").ok_or(Error::Missing)?;
        if name == "added" && self.added {
            return Ok(file(name, 0));
        }
        let index = name
            .strip_prefix("item")
            .and_then(|index| index.parse::<usize>().ok())
            .filter(|index| *index < CHILDREN)
            .ok_or(Error::Missing)?;
        Ok(file(
            name,
            if index == 0 {
                self.first_size
            } else {
                index as u64
            },
        ))
    }

    pub(super) fn read_directory(
        &mut self,
        path: &str,
        cursor: DirectoryCursor,
        output: &mut [u8],
    ) -> Result<usize, Error> {
        if path == "long-names" {
            let mut batch =
                DirectoryBatchBuilder::new(output, cursor).ok_or(Error::InvalidInput)?;
            // 86 UTF-16 code units are legal on FAT; 258 UTF-8 bytes exceed
            // Native's 255-byte directory record without violating the wire.
            let entry = file(&"界".repeat(86), 0);
            if !cursor.is_start()
                || !batch
                    .push(
                        &entry,
                        DirectoryCursor {
                            words: [1, 0, 0, 0],
                        },
                    )
                    .ok_or(Error::InvalidInput)?
            {
                return Err(Error::InvalidInput);
            }
            return batch.finish(true).ok_or(Error::InvalidInput);
        }
        if path != "entries" {
            return Err(Error::Unsupported);
        }
        self.reads += 1;
        if cursor.words[1..] != [0; 3] {
            return Err(Error::InvalidInput);
        }
        let mut index = cursor.words[0] as usize;
        let mut batch = DirectoryBatchBuilder::new(output, cursor).ok_or(Error::InvalidInput)?;
        while index < CHILDREN + usize::from(self.added) {
            let entry = if index == CHILDREN {
                file("added", 0)
            } else {
                file(
                    &format!("item{index:03}"),
                    if index == 0 {
                        self.first_size
                    } else {
                        index as u64
                    },
                )
            };
            let next = DirectoryCursor {
                words: [(index + 1) as u64, 0, 0, 0],
            };
            if !batch.push(&entry, next).ok_or(Error::InvalidInput)? {
                return batch.finish(false).ok_or(Error::InvalidInput);
            }
            index += 1;
        }
        batch.finish(true).ok_or(Error::InvalidInput)
    }

    pub(super) fn write(&mut self, path: &str, offset: u64, input: &[u8]) -> Result<usize, Error> {
        if path != "entries/item000" {
            return Err(Error::Unsupported);
        }
        self.first_size = self.first_size.max(
            offset
                .checked_add(input.len() as u64)
                .ok_or(Error::InvalidInput)?,
        );
        Ok(input.len())
    }

    pub(super) fn create(&mut self, path: &str, directory: bool) -> Result<(), Error> {
        if path != "entries/added" || directory || self.added {
            return Err(Error::Unsupported);
        }
        self.added = true;
        Ok(())
    }
}

fn file(name: &str, size: u64) -> Entry {
    let mut entry = Entry::empty();
    entry.kind = protocol::FILE;
    entry.name[..name.len()].copy_from_slice(name.as_bytes());
    entry.name_len = name.len();
    entry.size = size;
    entry
}
