// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Lossy recovery is separate from ordinary unmount and preserves old identities.
use super::*;
use std::io::Write;

pub(super) fn run(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    let root_id = root.info().map_err(show)?.location().mount().get();
    if !matches!(
        hyper_os::filesystem::detach_failed(root.as_handle_ref(), root_id),
        Err(hyper_os::Error::Status(hyper_os::Status::INVALID_ARGUMENT))
    ) {
        return Err("failed detach accepted the namespace root".into());
    }
    let worker = Worker::launch(startup, root, "/fs-failed-dirty")
        .map_err(|error| format!("launch failed-detach fixture: {error}"))?;
    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open("/fs-failed-dirty/a")
        .map_err(context("open dirty file"))?;
    let old_directory = root
        .open_directory("/fs-failed-dirty", DirectoryRights::READ)
        .map_err(context("open old mount directory"))?;
    file.write_all(&[1; 4096])
        .map_err(context("dirty cached page"))?;
    if !matches!(
        hyper_os::filesystem::detach_failed(root.as_handle_ref(), worker.mount_id),
        Err(hyper_os::Error::Status(hyper_os::Status::BUSY))
    ) {
        return Err("forced detach accepted a healthy provider".into());
    }
    worker.stop()?;
    if hyper_os::filesystem::unmount(root.as_handle_ref(), "/fs-failed-dirty").is_ok() {
        return Err("ordinary unmount discarded failed dirty data".into());
    }
    let discarded = hyper_os::filesystem::detach_failed(root.as_handle_ref(), worker.mount_id)
        .map_err(context("detach failed dirty mount"))?;
    if discarded != 4096 {
        return Err(format!("failed detach lost dirty accounting: {discarded}"));
    }
    file.seek(SeekFrom::Start(0)).map_err(show)?;
    if file.read(&mut [0; 1]).is_ok() || old_directory.open("a", FileRights::READ).is_ok() {
        return Err("old capability survived failed generation retirement".into());
    }
    let replacement = Worker::launch(startup, root, "/fs-failed-dirty")
        .map_err(|error| format!("launch replacement fixture: {error}"))?;
    if replacement.mount_id == worker.mount_id
        || hyper_os::filesystem::detach_failed(root.as_handle_ref(), worker.mount_id).is_ok()
    {
        return Err("old mount identity detached a replacement".into());
    }
    let mut fresh = File::open("/fs-failed-dirty/a").map_err(show)?;
    let mut byte = [0xff];
    fresh.read_exact(&mut byte).map_err(show)?;
    if byte != [0] {
        return Err("replacement reused abandoned dirty cache".into());
    }
    replacement.stop()?;
    hyper_os::filesystem::detach_failed(root.as_handle_ref(), replacement.mount_id)
        .map_err(show)?;

    let parent = Worker::launch(startup, root, "/fs-failed-parent")
        .map_err(|error| format!("launch parent fixture: {error}"))?;
    let child = Worker::launch(startup, root, "/fs-failed-parent/entries")
        .map_err(|error| format!("launch child fixture: {error}"))?;
    parent.stop()?;
    if !matches!(
        hyper_os::filesystem::detach_failed(root.as_handle_ref(), parent.mount_id),
        Err(hyper_os::Error::Status(hyper_os::Status::BUSY))
    ) {
        return Err("failed detach orphaned a nested mount".into());
    }
    child.stop()?;
    hyper_os::filesystem::detach_failed(root.as_handle_ref(), child.mount_id).map_err(show)?;
    hyper_os::filesystem::detach_failed(root.as_handle_ref(), parent.mount_id).map_err(show)?;
    println!("FS-FAILURE: failed-detach PASS");
    Ok(())
}
