// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

use super::*;
use std::sync::atomic::{AtomicUsize, Ordering};

struct Directory(std::path::PathBuf);

impl Directory {
    fn new() -> io::Result<Self> {
        static SERIAL: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "hyper-native-payload-{}-{}",
            std::process::id(),
            SERIAL.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path)?;
        Ok(Self(path))
    }
}

impl Drop for Directory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn entry(output: &mut Vec<u8>, name: &str, mode: u32, bytes: &[u8]) {
    output.extend_from_slice(b"070701");
    for value in [
        1,
        mode,
        0,
        0,
        1,
        0,
        bytes.len() as u32,
        0,
        0,
        0,
        0,
        name.len() as u32 + 1,
        0,
    ] {
        output.extend_from_slice(format!("{value:08x}").as_bytes());
    }
    output.extend_from_slice(name.as_bytes());
    output.push(0);
    while !output.len().is_multiple_of(4) {
        output.push(0);
    }
    output.extend_from_slice(bytes);
    while !output.len().is_multiple_of(4) {
        output.push(0);
    }
}

fn finish(mut archive: Vec<u8>) -> Vec<u8> {
    entry(&mut archive, "TRAILER!!!", 0, &[]);
    archive
}

#[test]
fn independent_copy_preserves_programs_and_relative_library_links() -> io::Result<()> {
    let directory = Directory::new()?;
    let mut archive = Vec::new();
    entry(&mut archive, ".", 0o040755, &[]);
    entry(&mut archive, "bin/tool", 0o100755, b"executable payload");
    entry(&mut archive, "lib64/runtime", 0o100644, b"library payload");
    entry(&mut archive, "lib", 0o120777, b"lib64");
    let archive = finish(archive);
    unpack(archive.as_slice(), &directory.0)?;
    assert_eq!(
        fs::read(directory.0.join("bin/tool"))?,
        b"executable payload"
    );
    assert_eq!(
        fs::metadata(directory.0.join("bin/tool"))?
            .permissions()
            .mode()
            & 0o777,
        0o755
    );
    assert_eq!(
        fs::read(directory.0.join("lib/runtime"))?,
        b"library payload"
    );
    Ok(())
}

#[test]
fn malformed_names_truncation_and_unsupported_nodes_are_rejected() -> io::Result<()> {
    for (name, mode, truncate) in [
        ("../escape", 0o100644, false),
        ("/absolute", 0o100644, false),
        ("device", 0o020644, false),
        ("file", 0o100644, true),
    ] {
        let directory = Directory::new()?;
        let mut archive = Vec::new();
        entry(&mut archive, name, mode, b"payload");
        let archive = if truncate {
            archive[..archive.len() - 4].to_vec()
        } else {
            finish(archive)
        };
        assert!(unpack(archive.as_slice(), &directory.0).is_err());
    }
    Ok(())
}

#[test]
fn symlink_cannot_redirect_a_later_payload_write() -> io::Result<()> {
    let directory = Directory::new()?;
    let outside = Directory::new()?;
    let mut archive = Vec::new();
    entry(
        &mut archive,
        "link",
        0o120777,
        outside.0.to_string_lossy().as_bytes(),
    );
    entry(&mut archive, "link/escaped", 0o100644, b"must not write");
    assert!(unpack(finish(archive).as_slice(), &directory.0).is_err());
    assert!(!outside.0.join("escaped").exists());
    Ok(())
}
