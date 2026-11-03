// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Short writes cross the real worker RPC, VFS, Native SDK and Rust std PAL.

use super::{Result, Worker, show};
use hyper_os::{
    fs::{Directory, FileRights},
    startup::Startup,
};
use std::{
    fs::OpenOptions,
    io::{Read, Seek, SeekFrom, Write},
};

pub(super) fn run(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    let worker = Worker::launch(startup, root, "/fs-partial")?;
    let mut input = [0x35; 1024];
    input[512..].fill(0xa9);
    let file = root
        .open("/fs-partial/partial", FileRights::WRITE)
        .map_err(show)?;
    if file.append(&input).map_err(show)? != (512, 512)
        || !matches!(
            file.append(&input[512..]),
            Err(hyper_os::Error::Status(hyper_os::Status::NO_SPACE))
        )
    {
        return Err("Native append lost partial progress or accepted data beyond capacity".into());
    }
    drop(file);

    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .open("/fs-partial/partial")
        .map_err(show)?;
    let mut actual = [0; 512];
    file.read_exact(&mut actual).map_err(show)?;
    if actual != input[..512] || file.metadata().map_err(show)?.len() != 512 {
        return Err("short append duplicated or discarded its prefix".into());
    }
    file.set_len(0).map_err(show)?;
    file.seek(SeekFrom::Start(0)).map_err(show)?;
    if !file
        .write_all(&input)
        .is_err_and(|error| error.kind() == std::io::ErrorKind::StorageFull)
        || file.stream_position().map_err(show)? != 512
    {
        return Err("write_all did not preserve its short-write position on ENOSPC".into());
    }
    file.seek(SeekFrom::Start(0)).map_err(show)?;
    file.read_exact(&mut actual).map_err(show)?;
    if actual != input[..512] || file.metadata().map_err(show)?.len() != 512 {
        return Err("write_all replayed accepted bytes".into());
    }
    file.set_len(0).map_err(show)?;
    drop(file);
    let mut appended = OpenOptions::new()
        .append(true)
        .open("/fs-partial/partial")
        .map_err(show)?;
    if appended.write(&input).map_err(show)? != 512
        || appended.stream_position().map_err(show)? != 512
        || !appended
            .write(&input[512..])
            .is_err_and(|error| error.kind() == std::io::ErrorKind::StorageFull)
        || appended.stream_position().map_err(show)? != 512
    {
        return Err("std append lost short-write progress".into());
    }
    drop(appended);
    worker.stop()?;
    println!("FS-FAILURE: partial-write PASS");

    let worker = Worker::launch(startup, root, "/fs-late-write")?;
    let mut file = OpenOptions::new()
        .write(true)
        .open("/fs-late-write/late")
        .map_err(show)?;
    file.write_all(&input[..512]).map_err(show)?;
    if file.stream_position().map_err(show)? != 512 || file.sync_all().is_ok() {
        return Err("accepted write hid its later durability failure".into());
    }
    worker.wait_retired()?;
    println!("FS-FAILURE: late-write-error PASS");
    Ok(())
}
