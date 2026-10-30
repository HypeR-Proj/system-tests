// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Bounded newc extraction for the test-generated independent root payload.

use std::fs;
use std::io::{self, Read};
#[cfg(target_os = "hyper")]
use std::os::hyper::fs::{PermissionsExt, symlink};
#[cfg(not(target_os = "hyper"))]
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Component, Path};

fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid Native test payload")
}

fn number(header: &[u8], start: usize) -> io::Result<u32> {
    u32::from_str_radix(
        std::str::from_utf8(&header[start..start + 8]).map_err(|_| invalid())?,
        16,
    )
    .map_err(|_| invalid())
}

fn padding(input: &mut impl Read, size: u32) -> io::Result<()> {
    let mut ignored = [0; 3];
    input.read_exact(&mut ignored[..((4 - size % 4) % 4) as usize])
}

pub fn unpack(mut input: impl Read, root: impl AsRef<Path>) -> io::Result<()> {
    let root = root.as_ref();
    let mut remaining = 512 * 1024 * 1024_u64;
    for _ in 0..4096 {
        let mut header = [0; 110];
        input.read_exact(&mut header)?;
        if &header[..6] != b"070701" {
            return Err(invalid());
        }
        let mode = number(&header, 14)?;
        let size = number(&header, 54)?;
        let name_size = number(&header, 94)?;
        if !(2..=4096).contains(&name_size) {
            return Err(invalid());
        }
        remaining = remaining.checked_sub(u64::from(size)).ok_or_else(invalid)?;
        let mut name = vec![0; name_size as usize];
        input.read_exact(&mut name)?;
        if name.pop() != Some(0) {
            return Err(invalid());
        }
        let name = std::str::from_utf8(&name).map_err(|_| invalid())?;
        padding(&mut input, 110 + name_size)?;
        if name == "TRAILER!!!" {
            return if size == 0 { Ok(()) } else { Err(invalid()) };
        }
        if name == "." && mode & 0o170000 == 0o040000 && size == 0 {
            continue;
        }
        let relative = Path::new(name);
        if relative
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
        {
            return Err(invalid());
        }
        let target = root.join(relative);
        // No previously extracted symlink may redirect a later write.
        let mut parent = target.parent();
        while let Some(path) = parent.filter(|path| *path != root) {
            if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
                return Err(invalid());
            }
            parent = path.parent();
        }
        fs::create_dir_all(target.parent().ok_or_else(invalid)?)?;
        match mode & 0o170000 {
            0o040000 if size == 0 => fs::create_dir_all(&target)?,
            0o100000 => {
                let mut output = fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&target)?;
                let count = io::copy(&mut input.by_ref().take(u64::from(size)), &mut output)?;
                if count != u64::from(size) {
                    return Err(invalid());
                }
                output.set_permissions(fs::Permissions::from_mode(mode & 0o777))?;
            }
            0o120000 if size <= 4096 => {
                let mut link = vec![0; size as usize];
                input.read_exact(&mut link)?;
                symlink(std::str::from_utf8(&link).map_err(|_| invalid())?, &target)?;
            }
            _ => return Err(invalid()),
        }
        padding(&mut input, size)?;
    }
    Err(invalid())
}

#[cfg(test)]
#[path = "native-init-payload-tests.rs"]
mod tests;
