// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0
//! RAM-core and mounted VFS acceptance; never formats a physical device.
use hyper_fs_f2fs::{Device, Error, FileSystem, INVALID};
use std::{
    io::{Read, Seek, SeekFrom, Write},
    path::Path,
};
const SIZE: usize = 4 * 1024 * 1024;
fn payload() -> Vec<u8> {
    (0..SIZE)
        .map(|i| ((i * 17 + i / 251) % 251) as u8)
        .collect()
}
fn show(e: impl std::fmt::Debug) -> String {
    format!("{e:?}")
}
struct Ram(Vec<u8>);
impl Device for Ram {
    fn blocks(&self) -> u64 {
        (self.0.len() / 4096) as u64
    }
    fn read(&mut self, offset: u64, output: &mut [u8]) -> Result<(), Error> {
        let start = usize::try_from(offset).map_err(|_| INVALID)?;
        let end = start.checked_add(output.len()).ok_or(INVALID)?;
        output.copy_from_slice(self.0.get(start..end).ok_or(INVALID)?);
        Ok(())
    }
    fn write(&mut self, offset: u64, input: &[u8]) -> Result<(), Error> {
        let start = usize::try_from(offset).map_err(|_| INVALID)?;
        let end = start.checked_add(input.len()).ok_or(INVALID)?;
        self.0
            .get_mut(start..end)
            .ok_or(INVALID)?
            .copy_from_slice(input);
        Ok(())
    }
    fn flush(&mut self) -> Result<(), Error> {
        Ok(())
    }
}
fn ram() -> Result<(), String> {
    let mut device = Ram(vec![0; 128 * 1024 * 1024]);
    FileSystem::format(&mut device, [0x53; 16]).map_err(show)?;
    let mut fs = FileSystem::mount(device).map_err(show)?;
    fs.create("dir", true).map_err(show)?;
    fs.create("dir/data", false).map_err(show)?;
    let bytes = payload();
    for (offset, part) in bytes.chunks(128 * 1024).enumerate() {
        let actual = fs
            .write("dir/data", (offset * 128 * 1024) as u64, part)
            .map_err(show)?;
        if actual != part.len() {
            return Err("short write".into());
        }
    }
    fs.sync().map_err(show)?;
    fs.rename("dir/data", "dir/renamed").map_err(show)?;
    let device = fs.close().map_err(show)?;
    let mut fs = FileSystem::mount(device).map_err(show)?;
    let mut output = vec![0; SIZE];
    if fs.read("dir/renamed", 0, &mut output).map_err(show)? != SIZE || output != bytes {
        return Err("RAM remount mismatch".into());
    }
    fs.resize("dir/renamed", 17).map_err(show)?;
    fs.resize("dir/renamed", 9000).map_err(show)?;
    let actual = fs.read("dir/renamed", 0, &mut output).map_err(show)?;
    if actual != 9000 || output[..17] != bytes[..17] || output[17..9000].iter().any(|b| *b != 0) {
        return Err("RAM truncate mismatch".into());
    }
    fs.close().map_err(show)?;
    Ok(())
}
fn mounted(mode: &str, root: &Path) -> Result<(), String> {
    let dir = root.join("f2fs-acceptance");
    let file = dir.join("payload");
    let bytes = payload();
    match mode {
        "write" => {
            std::fs::create_dir(&dir).map_err(show)?;
            let mut f = std::fs::File::create(&file).map_err(show)?;
            for (index, part) in bytes.chunks(128 * 1024).enumerate() {
                f.write_all(part)
                    .map_err(|e| format!("write chunk {index}: {e:?}"))?;
            }
            f.sync_all().map_err(show)?;
            drop(f);
            std::fs::rename(&file, dir.join("renamed")).map_err(show)?;
            for i in 0..80 {
                std::fs::write(dir.join(format!("file-{i}")), b"entry").map_err(show)?;
            }
            let count = std::fs::read_dir(&dir)
                .map_err(show)?
                .collect::<Result<Vec<_>, _>>()
                .map_err(show)?
                .len();
            if count != 81 {
                return Err(format!("directory count {count}"));
            }
        }
        "writeback" => {
            let file = root.join("f2fs-writeback");
            let chunk = &bytes[..128 * 1024];
            let mut f = std::fs::File::create(&file).map_err(show)?;
            // Exceed the core's 8 MiB threshold before issuing an explicit sync.
            for index in 0..96 {
                f.write_all(chunk)
                    .map_err(|e| format!("writeback chunk {index}: {e:?}"))?;
            }
            f.sync_all().map_err(show)?;
            drop(f);
            let mut f = std::fs::File::open(&file).map_err(show)?;
            let mut read = vec![0; chunk.len()];
            for index in 0..96 {
                f.read_exact(&mut read).map_err(show)?;
                if read != chunk {
                    return Err(format!("writeback mismatch at chunk {index}"));
                }
            }
            drop(f);
            std::fs::remove_file(file).map_err(show)?;
        }
        "verify" => {
            let renamed = dir.join("renamed");
            let content = std::fs::read(&renamed).map_err(|e| format!("cold read: {e:?}"))?;
            if content != bytes {
                return Err("cold remount payload mismatch".into());
            }
            let mut f = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(&renamed)
                .map_err(show)?;
            f.set_len(17).map_err(|e| format!("truncate: {e:?}"))?;
            f.set_len(9000).map_err(|e| format!("regrow: {e:?}"))?;
            f.seek(SeekFrom::Start(0)).map_err(show)?;
            let mut out = Vec::new();
            f.read_to_end(&mut out).map_err(show)?;
            if out.len() != 9000 || out[..17] != bytes[..17] || out[17..].iter().any(|b| *b != 0) {
                return Err("truncate/regrow mismatch".into());
            }
            f.sync_all().map_err(show)?;
            drop(f);
            let nested = dir.join("nested/child");
            std::fs::create_dir_all(&nested).map_err(show)?;
            std::fs::write(nested.join("data"), b"nested").map_err(show)?;
            if std::fs::read(nested.join("data")).map_err(show)? != b"nested" {
                return Err("nested payload mismatch".into());
            }
            std::fs::remove_dir_all(&dir).map_err(|e| format!("remove tree: {e:?}"))?;
        }
        _ => {
            return Err("usage: f2fs-smoke ram | write ROOT | verify ROOT | writeback ROOT".into());
        }
    }
    Ok(())
}
fn main() -> std::process::ExitCode {
    let mode = std::env::args().nth(1).unwrap_or_else(|| "ram".into());
    let result = if mode == "ram" {
        ram()
    } else {
        mounted(
            &mode,
            Path::new(&std::env::args().nth(2).unwrap_or_else(|| "/data".into())),
        )
    };
    match result {
        Ok(()) => {
            println!("F2FS-SMOKE: {mode} PASS");
            std::process::ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("F2FS-SMOKE: {mode} FAIL {e}");
            std::process::ExitCode::FAILURE
        }
    }
}
