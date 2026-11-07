// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! A third receipt must be admitted while the second completion is still held.
use super::*;
use hyper_filesystem::protocol::{self, Entry, Error};
use hyper_fs_service::{SubmittedWrite, Volume, WriteCompletion, WritebackPolicy};
use hyper_os::fs::SyncScope;
use std::io::Write;
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
    mpsc,
};

const BATCH: usize = protocol::DATA_BYTES;
const BATCHES: usize = 5;
const BYTES: usize = BATCH * BATCHES;
const NORMAL: &str = "/fs-writeback-rolling";
const FAILURE: &str = "/fs-writeback-rolling-failure";

pub(super) fn handles(path: &str) -> bool {
    matches!(path, NORMAL | FAILURE)
}

pub(super) struct RollingVolume {
    notice: Arc<OwnedHandle<ByteChannelObject>>,
    reaped: Arc<AtomicUsize>,
    submitted: usize,
    fail: bool,
}
impl RollingVolume {
    pub(super) fn new(notice: OwnedHandle<ByteChannelObject>, path: &str) -> Self {
        Self {
            notice: Arc::new(notice),
            reaped: Arc::new(AtomicUsize::new(0)),
            submitted: 0,
            fail: path == FAILURE,
        }
    }
}
struct Completion {
    notice: Arc<OwnedHandle<ByteChannelObject>>,
    reaped: Arc<AtomicUsize>,
    number: usize,
    fail: bool,
    retired: bool,
}
impl WriteCompletion for Completion {
    fn wait(mut self: Box<Self>) -> std::result::Result<(), Error> {
        if self.number <= 2 {
            receive(
                &self.notice,
                if self.number == 1 {
                    b"release1"
                } else {
                    b"release2"
                },
            )
            .map_err(|_| Error::Closed)?;
        }
        if self
            .reaped
            .compare_exchange(
                self.number - 1,
                self.number,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            return Err(Error::Corrupt);
        }
        self.retired = true;
        if self.number == 3 {
            self.notice
                .as_byte_channel()
                .send(b"reaped3")
                .map_err(|_| Error::Closed)?;
        }
        if self.number == 2 && self.fail {
            Err(Error::Io)
        } else {
            Ok(())
        }
    }
}
impl Drop for Completion {
    fn drop(&mut self) {
        // A terminal completion failure closes the service actor. Its queued
        // third receipt is then dropped instead of receiving another Reap RPC.
        // This fixture owns no physical I/O; this notice proves local receipt
        // cleanup only, never successful completion of its accepted bytes.
        if self.number == 3 && !self.retired && self.reaped.load(Ordering::Acquire) == 2 {
            let _ = self.notice.as_byte_channel().send(b"dropped3");
        }
    }
}

fn path_check(path: &str) -> std::result::Result<(), Error> {
    if path == "payload" {
        Ok(())
    } else {
        Err(Error::Missing)
    }
}
impl Volume for RollingVolume {
    fn capabilities(&self) -> protocol::Capabilities {
        protocol::Capabilities::ASYNC_WRITEBACK
    }
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
        output[..count].fill(if self.reaped.load(Ordering::Acquire) == BATCHES {
            0xa5
        } else {
            0
        });
        Ok(count)
    }
    fn write(&mut self, _: &str, _: u64, _: &[u8]) -> std::result::Result<usize, Error> {
        Err(Error::Corrupt)
    }
    fn submit_writeback(
        &mut self,
        path: &str,
        offset: u64,
        input: &[u8],
        _: u64,
    ) -> std::result::Result<SubmittedWrite, Error> {
        path_check(path)?;
        if input.len() != BATCH
            || offset != (self.submitted * BATCH) as u64
            || input.iter().any(|b| *b != 0xa5)
            || self.submitted == BATCHES
        {
            return Err(Error::Corrupt);
        }
        self.submitted += 1;
        if self.submitted == 2 || self.submitted == 3 {
            self.notice
                .as_byte_channel()
                .send(if self.submitted == 2 {
                    b"accepted2"
                } else {
                    b"accepted3"
                })
                .map_err(|_| Error::Closed)?;
        }
        Ok(SubmittedWrite {
            accepted: input.len(),
            completion: Box::new(Completion {
                notice: self.notice.clone(),
                reaped: self.reaped.clone(),
                number: self.submitted,
                fail: self.fail,
                retired: false,
            }),
        })
    }
    fn sync(&mut self) -> std::result::Result<(), Error> {
        if self.fail || self.submitted != BATCHES || self.reaped.load(Ordering::Acquire) != BATCHES
        {
            Err(Error::Corrupt)
        } else {
            Ok(())
        }
    }
    fn set_times(
        &mut self,
        path: &str,
        _: Option<hyper_filesystem::Timestamp>,
        _: Option<hyper_filesystem::Timestamp>,
    ) -> std::result::Result<(), Error> {
        path_check(path)?;
        // A tail cannot publish metadata while an older receipt remains live.
        self.sync()
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
    for path in [NORMAL, FAILURE] {
        scenario(startup, root, path).map_err(|error| format!("{path}: {error}"))?;
    }
    println!("FS-FAILURE: writeback-rolling PASS");
    Ok(())
}
fn scenario(startup: &Startup<'_>, root: &Directory, path: &str) -> Result<()> {
    let worker = Worker::launch(startup, root, path)?;
    let filename = format!("{path}/payload");
    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&filename)
        .map_err(show)?;
    let bytes = vec![0xa5; 64 * 1024];
    for _ in 0..BYTES / bytes.len() {
        file.write_all(&bytes).map_err(show)?;
    }
    let fence = root.open(&filename, FileRights::WRITE).map_err(show)?;
    std::thread::scope(|scope| -> Result<()> {
        let (sender, receiver) = mpsc::channel();
        let fence = &fence;
        let syncing = scope.spawn(move || {
            let _ = sender.send(fence.sync(SyncScope::Volume).map_err(show));
        });
        let result = (|| -> Result<()> {
            receive(&worker.notice, b"accepted2")?;
            worker
                .notice
                .as_byte_channel()
                .send(b"release1")
                .map_err(show)?;
            // Completion 2 remains blocked until this proves slot 1 was refilled.
            receive(&worker.notice, b"accepted3")?;
            worker
                .notice
                .as_byte_channel()
                .send(b"release2")
                .map_err(show)?;
            receive(
                &worker.notice,
                if path == FAILURE {
                    b"dropped3"
                } else {
                    b"reaped3"
                },
            )?;
            let result = receiver
                .recv_timeout(Duration::from_secs(10))
                .map_err(|_| "rolling sync failed to finish")?;
            if (path == NORMAL) != result.is_ok() {
                return Err(format!("unexpected rolling outcome: {result:?}"));
            }
            Ok(())
        })();
        if result.is_err() {
            let _ = worker.stop();
        }
        syncing.join().map_err(|_| "rolling sync thread panicked")?;
        result
    })?;
    if path == FAILURE {
        if hyper_os::filesystem::unmount(root.as_handle_ref(), path).is_ok() {
            return Err("failed rolling mount unmounted normally".into());
        }
        let discarded = hyper_os::filesystem::detach_failed(root.as_handle_ref(), worker.mount_id)
            .map_err(show)?;
        // Receipt 1 retired successfully before receipt 2 failed; its pages
        // must not be charged again. Accepted 2/3 and frozen 4/5 remain owned.
        if discarded != (BYTES - BATCH) as u64 {
            return Err(format!("wrong failed rolling bytes: {discarded}"));
        }
    } else {
        file.seek(SeekFrom::Start(0)).map_err(show)?;
        let mut read = vec![0; bytes.len()];
        for _ in 0..BYTES / read.len() {
            file.read_exact(&mut read).map_err(show)?;
            if read != bytes {
                return Err("rolling completion lost written bytes".into());
            }
        }
    }
    drop((file, fence));
    worker.stop()?;
    Ok(())
}
