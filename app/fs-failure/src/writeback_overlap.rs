// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Hold real RPC write completions while checking cache progress and finite sync cuts.
use super::*;
use hyper_filesystem::protocol::{self, Entry, Error};
use hyper_fs_service::{SubmittedWrite, Volume, WriteCompletion, WritebackPolicy};
use hyper_os::fs::SyncScope;
use std::io::Write;
use std::sync::{Arc, Condvar, Mutex, mpsc};

const PAGE: usize = 4096;
const MOUNT: &str = "/fs-writeback-overlap";

struct Media {
    pages: [Vec<u8>; 3],
    released: bool,
    modified: [Option<hyper_filesystem::Timestamp>; 3],
}

pub(super) struct OverlapVolume {
    notice: Arc<OwnedHandle<ByteChannelObject>>,
    media: Arc<(Mutex<Media>, Condvar)>,
    submitted: usize,
    synced: usize,
    stats: [usize; 3],
    initial_stats: Option<[usize; 3]>,
}

fn index(path: &str) -> std::result::Result<usize, Error> {
    match path {
        "a" => Ok(0),
        "b" => Ok(1),
        "c" => Ok(2),
        _ => Err(Error::Missing),
    }
}

struct Completion(Option<std::thread::JoinHandle<std::result::Result<(), Error>>>);
impl WriteCompletion for Completion {
    fn wait(mut self: Box<Self>) -> std::result::Result<(), Error> {
        self.0
            .take()
            .ok_or(Error::Corrupt)?
            .join()
            .map_err(|_| Error::Io)?
    }
}

impl OverlapVolume {
    pub(super) fn new(notice: OwnedHandle<ByteChannelObject>) -> Self {
        Self {
            notice: Arc::new(notice),
            media: Arc::new((
                Mutex::new(Media {
                    pages: std::array::from_fn(|_| vec![0; PAGE]),
                    released: false,
                    modified: [None; 3],
                }),
                Condvar::new(),
            )),
            submitted: 0,
            synced: 0,
            stats: [0; 3],
            initial_stats: None,
        }
    }
}

impl Volume for OverlapVolume {
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
            let i = index(path)?;
            self.stats[i] += 1;
            entry.kind = protocol::FILE;
            let media = self.media.0.lock().map_err(|_| Error::Io)?;
            entry.size = media.pages[i].len() as u64;
            entry.modified = media.modified[i];
            entry.name[0] = path.as_bytes()[0];
            entry.name_len = 1;
        }
        Ok(entry)
    }
    fn read(
        &mut self,
        path: &str,
        offset: u64,
        out: &mut [u8],
    ) -> std::result::Result<usize, Error> {
        let i = index(path)?;
        let state = self.media.0.lock().map_err(|_| Error::Io)?;
        let start = usize::try_from(offset)
            .map_err(|_| Error::InvalidInput)?
            .min(state.pages[i].len());
        let length = out.len().min(state.pages[i].len() - start);
        out[..length].copy_from_slice(&state.pages[i][start..start + length]);
        Ok(length)
    }
    fn write(&mut self, _: &str, _: u64, _: &[u8]) -> std::result::Result<usize, Error> {
        // This fixture must exercise the negotiated asynchronous path.
        Err(Error::Corrupt)
    }
    fn submit_writeback(
        &mut self,
        path: &str,
        offset: u64,
        input: &[u8],
        _: u64,
    ) -> std::result::Result<SubmittedWrite, Error> {
        let i = index(path)?;
        let offset = usize::try_from(offset).map_err(|_| Error::InvalidInput)?;
        if !offset.is_multiple_of(PAGE)
            || !input.len().is_multiple_of(PAGE)
            || offset + input.len() > PAGE * 2
        {
            return Err(Error::Corrupt);
        }
        let bytes = input.to_vec();
        let mut receipt = Box::new(Completion(None));
        let media = self.media.clone();
        let notice = self.notice.clone();
        let first = self.submitted == 0;
        let structural = self.synced >= 2;
        if first {
            self.initial_stats = Some(self.stats);
        }
        // Completion starts before acceptance and never depends on receipt.wait().
        receipt.0 = Some(
            std::thread::Builder::new()
                .spawn(move || {
                    if structural {
                        notice
                            .as_byte_channel()
                            .send(b"fence-pending")
                            .map_err(|_| Error::Closed)?;
                        receive(&notice, b"fence-release").map_err(|_| Error::Closed)?;
                    }
                    if first {
                        let result = receive(&notice, b"release").map_err(|_| Error::Closed);
                        let mut state = media.0.lock().map_err(|_| Error::Io)?;
                        state.released = true;
                        media.1.notify_all();
                        result?;
                    }
                    let mut state = media.0.lock().map_err(|_| Error::Io)?;
                    while !state.released {
                        state = media.1.wait(state).map_err(|_| Error::Io)?;
                    }
                    if state.pages[i].len() < offset + bytes.len() {
                        state.pages[i].resize(offset + bytes.len(), 0);
                    }
                    state.pages[i][offset..offset + bytes.len()].copy_from_slice(&bytes);
                    Ok(())
                })
                .map_err(|_| Error::Allocation)?,
        );
        self.submitted += 1;
        if self.submitted == 2 {
            self.notice
                .as_byte_channel()
                .send(b"accepted2")
                .map_err(|_| Error::Closed)?;
        }
        Ok(SubmittedWrite {
            accepted: input.len(),
            completion: receipt,
        })
    }
    fn sync(&mut self) -> std::result::Result<(), Error> {
        if self.initial_stats != Some(self.stats) {
            return Err(Error::Corrupt);
        }
        let expected = if self.synced == 0 {
            [1, 2, 0]
        } else {
            [1, 7, 9]
        };
        {
            let state = self.media.0.lock().map_err(|_| Error::Io)?;
            for (page, expected) in state.pages.iter().zip(expected) {
                if page[..PAGE].iter().any(|byte| *byte != expected) {
                    return Err(Error::Corrupt);
                }
            }
            if self.synced != 0
                && (state.pages[2].len() != PAGE * 2
                    || state.pages[2][PAGE..].iter().any(|byte| *byte != 10))
            {
                return Err(Error::Corrupt);
            }
        }
        if self.synced == 0 {
            self.notice
                .as_byte_channel()
                .send(b"checkpoint")
                .map_err(|_| Error::Closed)?;
            receive(&self.notice, b"checkpoint-release").map_err(|_| Error::Closed)?;
        }
        self.synced += 1;
        Ok(())
    }
    fn set_times(
        &mut self,
        path: &str,
        _: Option<hyper_filesystem::Timestamp>,
        modified: Option<hyper_filesystem::Timestamp>,
    ) -> std::result::Result<(), Error> {
        let i = index(path)?;
        self.media.0.lock().map_err(|_| Error::Io)?.modified[i] = modified;
        Ok(())
    }
    fn read_directory(
        &mut self,
        path: &str,
        cursor: protocol::DirectoryCursor,
        output: &mut [u8],
    ) -> std::result::Result<usize, Error> {
        if !path.is_empty() || cursor.words[1..] != [0; 3] || cursor.words[0] > 3 {
            return Err(Error::InvalidInput);
        }
        let mut batch =
            protocol::DirectoryBatchBuilder::new(output, cursor).ok_or(Error::InvalidInput)?;
        let media = self.media.0.lock().map_err(|_| Error::Io)?;
        for i in cursor.words[0] as usize..3 {
            let mut entry = Entry::empty();
            entry.name[0] = b'a' + i as u8;
            entry.name_len = 1;
            entry.kind = protocol::FILE;
            entry.size = media.pages[i].len() as u64;
            entry.modified = media.modified[i];
            let next = protocol::DirectoryCursor {
                words: [i as u64 + 1, 0, 0, 0],
            };
            if !batch.push(&entry, next).ok_or(Error::InvalidInput)? {
                return batch.finish(false).ok_or(Error::InvalidInput);
            }
        }
        batch.finish(true).ok_or(Error::InvalidInput)
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
    fn resize(&mut self, path: &str, length: u64) -> std::result::Result<(), Error> {
        let i = index(path)?;
        let mut media = self.media.0.lock().map_err(|_| Error::Io)?;
        if i != 2 || length != PAGE as u64 || media.pages[i][..PAGE] != [11; PAGE] {
            return Err(Error::Corrupt);
        }
        // This mutation is valid only after the accepted older write completed.
        media.pages[i].truncate(PAGE);
        Ok(())
    }
}

pub(super) fn run(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    let worker = Worker::launch(startup, root, MOUNT)?;
    let open = |name| -> Result<File> {
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(format!("{MOUNT}/{name}"))
            .map_err(show)?;
        file.read_exact(&mut [0; PAGE]).map_err(show)?;
        file.seek(SeekFrom::Start(0)).map_err(show)?;
        Ok(file)
    };
    let mut a = open("a")?;
    let mut b = open("b")?;
    let mut c = open("c")?;
    check_listing(root, PAGE as u64)?;
    a.write_all(&[1; PAGE]).map_err(show)?;
    b.write_all(&[2; PAGE]).map_err(show)?;
    // Retain metadata before the stall, so progress cannot require a new Stat RPC.
    b.metadata().map_err(show)?;
    c.metadata().map_err(show)?;
    let fence = root
        .open(&format!("{MOUNT}/a"), FileRights::WRITE)
        .map_err(show)?;
    let mut checkpoint_file = c.try_clone().map_err(show)?;
    std::thread::scope(|scope| -> Result<()> {
        let (sync_sender, sync_receiver) = mpsc::channel();
        let fence = &fence;
        let sync = scope.spawn(move || {
            let _ = sync_sender.send(fence.sync(SyncScope::Volume).map_err(show));
        });
        let b = &mut b;
        let c = &mut c;
        let result = (|| -> Result<()> {
            receive(&worker.notice, b"accepted2")?;
            let (sender, receiver) = mpsc::channel();
            scope.spawn(move || {
                let outcome = (|| -> Result<()> {
                    b.seek(SeekFrom::Start(0)).map_err(show)?;
                    b.write_all(&[7; PAGE]).map_err(show)?;
                    c.write_all(&[9; PAGE]).map_err(show)?;
                    for (file, value) in [(b, 7), (c, 9)] {
                        file.seek(SeekFrom::Start(0)).map_err(show)?;
                        let mut bytes = [0; PAGE];
                        file.read_exact(&mut bytes).map_err(show)?;
                        if bytes.iter().any(|byte| *byte != value) {
                            return Err("cache read missed a newer dirty version".into());
                        }
                    }
                    Ok(())
                })();
                let _ = sender.send(outcome);
            });
            receiver
                .recv_timeout(Duration::from_secs(5))
                .map_err(|_| "cache reads or writes blocked behind in-flight writeback")??;
            worker
                .notice
                .as_byte_channel()
                .send(b"release")
                .map_err(show)?;
            receive(&worker.notice, b"checkpoint")?;
            let (sender, receiver) = mpsc::channel();
            scope.spawn(move || {
                let outcome = (|| -> Result<()> {
                    checkpoint_file
                        .seek(SeekFrom::Start(PAGE as u64))
                        .map_err(show)?;
                    checkpoint_file.write_all(&[10; PAGE]).map_err(show)?;
                    checkpoint_file
                        .seek(SeekFrom::Start(PAGE as u64))
                        .map_err(show)?;
                    let mut bytes = [0; PAGE];
                    checkpoint_file.read_exact(&mut bytes).map_err(show)?;
                    if bytes != [10; PAGE] {
                        return Err("cache read lost extension during checkpoint".into());
                    }
                    Ok(())
                })();
                let _ = sender.send(outcome);
            });
            receiver
                .recv_timeout(Duration::from_secs(5))
                .map_err(|_| "cache extension blocked behind checkpoint")??;
            worker
                .notice
                .as_byte_channel()
                .send(b"checkpoint-release")
                .map_err(show)?;
            sync_receiver
                .recv_timeout(Duration::from_secs(10))
                .map_err(|_| "sync did not finish after releasing completions")??;
            sync.join().map_err(|_| "sync thread panicked")?;
            Ok(())
        })();
        if result.is_err() {
            // Unblock RPC continuations before scoped joins on every failure path.
            let _ = worker.stop();
        }
        result
    })?;
    let modified = c.metadata().map_err(show)?.modified().map_err(show)?;
    for (file, value, length) in [(&mut b, 7, PAGE), (&mut c, 9, PAGE * 2)] {
        if file.metadata().map_err(show)?.len() != length as u64 {
            return Err("dirty metadata overlay changed file length".into());
        }
        file.seek(SeekFrom::Start(0)).map_err(show)?;
        let mut bytes = [0; PAGE];
        file.read_exact(&mut bytes).map_err(show)?;
        if bytes.iter().any(|byte| *byte != value) {
            return Err("old completion retired a newer dirty version".into());
        }
    }
    a.metadata().map_err(show)?;
    check_listing(root, (PAGE * 2) as u64)?;
    fence.sync(SyncScope::Volume).map_err(show)?;
    if c.metadata().map_err(show)?.modified().map_err(show)? != modified {
        return Err("writeback changed the accepted modification time".into());
    }
    check_listing(root, (PAGE * 2) as u64)?;
    c.seek(SeekFrom::Start(0)).map_err(show)?;
    c.write_all(&[11; PAGE]).map_err(show)?;
    std::thread::scope(|scope| -> Result<()> {
        let (sender, receiver) = mpsc::channel();
        let file = &c;
        scope.spawn(move || {
            let _ = sender.send(file.set_len(PAGE as u64).map_err(show));
        });
        let result = (|| -> Result<()> {
            receive(&worker.notice, b"fence-pending")?;
            worker
                .notice
                .as_byte_channel()
                .send(b"fence-release")
                .map_err(show)?;
            receiver
                .recv_timeout(Duration::from_secs(10))
                .map_err(|_| "truncate did not retire accepted writeback")??;
            Ok(())
        })();
        if result.is_err() {
            let _ = worker.stop();
        }
        result
    })?;
    if c.metadata().map_err(show)?.len() != PAGE as u64 {
        return Err("truncate retained pre-fence file length".into());
    }
    c.seek(SeekFrom::Start(0)).map_err(show)?;
    let mut bytes = [0; PAGE];
    c.read_exact(&mut bytes).map_err(show)?;
    if bytes != [11; PAGE] {
        return Err("truncate raced accepted write data".into());
    }
    check_listing(root, PAGE as u64)?;
    drop((a, b, c, fence));
    worker.stop()?;
    println!("FS-FAILURE: writeback-overlap PASS");
    Ok(())
}

fn check_listing(root: &Directory, length: u64) -> Result<()> {
    let directory = root
        .open_directory(MOUNT, DirectoryRights::READ)
        .map_err(show)?;
    let mut reader = directory.reader().map_err(show)?;
    while let Some(page) = reader.next_page().map_err(show)? {
        for entry in page.entries() {
            if entry.name_bytes() == b"c" {
                if entry.size() != length {
                    return Err("directory attributes missed cached or completed growth".into());
                }
                return Ok(());
            }
        }
    }
    Err("directory listing lost the written file".into())
}
