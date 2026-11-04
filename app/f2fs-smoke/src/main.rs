// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0
//! Mounted filesystem acceptance through the Native VFS and F2FS worker.
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
            // Include rename and directory creation in the acknowledged checkpoint.
            std::fs::OpenOptions::new()
                .write(true)
                .open(dir.join("renamed"))
                .map_err(|e| format!("open checkpoint handle: {e:?}"))?
                .sync_all()
                .map_err(|e| format!("checkpoint before restart: {e:?}"))?;
        }
        "periodic-idle" | "periodic-busy" => {
            let file = root.join("f2fs-periodic");
            std::fs::write(&file, &bytes[..16384]).map_err(show)?;
            // Neither close nor this fixture requests sync. The worker's
            // default 30-second checkpoint must cover the preceding payload.
            if mode == "periodic-idle" {
                std::thread::sleep(std::time::Duration::from_secs(35));
            } else {
                use std::io::{Seek, SeekFrom};
                let mut noise =
                    std::fs::File::create(root.join("f2fs-periodic-noise")).map_err(show)?;
                let start = std::time::Instant::now();
                while start.elapsed() < std::time::Duration::from_secs(35) {
                    noise.seek(SeekFrom::Start(0)).map_err(show)?;
                    noise.write_all(&bytes[..4096]).map_err(show)?;
                }
            }
        }
        "periodic-verify" => {
            let file = root.join("f2fs-periodic");
            if std::fs::read(&file).map_err(show)? != bytes[..16384] {
                return Err("periodic checkpoint lost payload".into());
            }
            std::fs::remove_file(file).map_err(show)?;
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
        "stream-write" => {
            let file = root.join("f2fs-stream");
            let chunk = &bytes[..128 * 1024];
            let mut f = std::fs::File::create(&file).map_err(show)?;
            // Exceed the old clean-page retention failure under the normal quota.
            for index in 0..384 {
                f.write_all(chunk)
                    .map_err(|e| format!("stream write {index}: {e:?}"))?;
            }
            f.sync_all().map_err(show)?;
        }
        "stream-verify" => {
            let file = root.join("f2fs-stream");
            let chunk = &bytes[..128 * 1024];
            let mut read = vec![0; chunk.len()];
            for pass in 0..2 {
                let mut f = std::fs::File::open(&file).map_err(show)?;
                for index in 0..384 {
                    f.read_exact(&mut read)
                        .map_err(|e| format!("stream read {pass}/{index}: {e:?}"))?;
                    if read != chunk {
                        return Err(format!("stream mismatch {pass}/{index}"));
                    }
                }
            }
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
            return Err(
                "usage: f2fs-smoke MODE ROOT (write, verify, writeback, stream-write, stream-verify, periodic-idle, periodic-busy, periodic-verify)".into(),
            );
        }
    }
    Ok(())
}
fn main() -> std::process::ExitCode {
    let mode = std::env::args().nth(1).unwrap_or_default();
    let result = mounted(
        &mode,
        Path::new(&std::env::args().nth(2).unwrap_or_else(|| "/data".into())),
    );
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
