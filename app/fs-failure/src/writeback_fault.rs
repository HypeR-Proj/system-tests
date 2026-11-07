// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Accepted bytes remain owned by a failed mount after ambiguous or forged replies.
use super::*;
use hyper_filesystem::protocol::{self, Entry, Operation, Request, Response};
use hyper_os::fs::SyncScope;
use std::{io::Write, sync::mpsc};

const PAGE: usize = 4096;
const LOST_ACK: &str = "/fs-writeback-lost-ack";
const BAD_COMPLETION: &str = "/fs-writeback-bad-completion";
const POST_COMPLETION: &str = "/fs-writeback-post-completion";

pub(super) fn handles(path: &str) -> bool {
    matches!(path, LOST_ACK | BAD_COMPLETION | POST_COMPLETION)
}

pub(super) fn run(startup: &Startup<'_>, root: &Directory) -> Result<()> {
    for path in [LOST_ACK, BAD_COMPLETION, POST_COMPLETION] {
        scenario(startup, root, path).map_err(|error| format!("{path}: {error}"))?;
    }
    println!("FS-FAILURE: writeback-fault PASS");
    Ok(())
}

fn scenario(startup: &Startup<'_>, root: &Directory, path: &str) -> Result<()> {
    let worker = Worker::launch(startup, root, path)?;
    let filename = format!("{path}/a");
    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&filename)
        .map_err(show)?;
    let old_directory = root
        .open_directory(path, DirectoryRights::READ)
        .map_err(show)?;
    file.write_all(&[0xa5; PAGE]).map_err(show)?;
    let fence = root.open(&filename, FileRights::WRITE).map_err(show)?;
    std::thread::scope(|scope| -> Result<()> {
        let (sender, receiver) = mpsc::channel();
        let sync_fence = &fence;
        let syncing = scope.spawn(move || {
            let result = sync_fence.sync(SyncScope::Volume).map_err(show);
            let _ = sender.send(result);
        });
        let result = (|| -> Result<()> {
            // The worker has copied and checked the entire input before this notice.
            receive(&worker.notice, b"accepted")?;
            if path == LOST_ACK {
                // Lose the ACK by killing the worker after actual input consumption.
                // The unresolved request must not be replayed or mistaken for success.
                worker.stop()?;
            } else if path == POST_COMPLETION {
                receive(&worker.notice, b"rejected")?;
            } else {
                // Distinguish the intended forged record from an incidental
                // worker failure before the malformed reply was published.
                receive(&worker.notice, b"forged")?;
            }
            let result = receiver
                .recv_timeout(Duration::from_secs(10))
                .map_err(|_| "writeback fault did not terminate the pending sync")?;
            if result.is_ok() {
                return Err("ambiguous or forged writeback reply reported sync success".into());
            }
            Ok(())
        })();
        if result.is_err() {
            // Close the endpoint before scoped joins, including notice/time-out failures.
            let _ = worker.stop();
        }
        syncing.join().map_err(|_| "fault sync thread panicked")?;
        result
    })?;

    if hyper_os::filesystem::unmount(root.as_handle_ref(), path).is_ok() {
        return Err("ordinary unmount discarded uncertain accepted bytes".into());
    }
    let discarded = hyper_os::filesystem::detach_failed(root.as_handle_ref(), worker.mount_id)
        .map_err(context("detach uncertain writeback"))?;
    if discarded != PAGE as u64 {
        return Err(format!("accepted writeback lost dirty charge: {discarded}"));
    }
    // The forged-completion worker is deliberately still alive here. Failure is
    // a protocol/health decision, not dependent on process death detection.
    if path != LOST_ACK {
        worker.stop()?;
    }
    let replacement = Worker::launch(startup, root, path)?;
    if replacement.mount_id == worker.mount_id
        || hyper_os::filesystem::detach_failed(root.as_handle_ref(), worker.mount_id).is_ok()
    {
        return Err("old writeback generation detached the replacement".into());
    }
    file.seek(SeekFrom::Start(0)).map_err(show)?;
    if file.read(&mut [0; 1]).is_ok()
        || old_directory.open("a", FileRights::READ).is_ok()
        || fence.sync(SyncScope::Volume).is_ok()
    {
        return Err("old writeback capability survived replacement".into());
    }
    let mut fresh = File::open(filename).map_err(show)?;
    let mut bytes = [0xff; PAGE];
    fresh.read_exact(&mut bytes).map_err(show)?;
    if bytes != [0; PAGE] {
        return Err("replacement reused abandoned accepted bytes".into());
    }
    replacement.stop()?;
    let discarded = hyper_os::filesystem::detach_failed(root.as_handle_ref(), replacement.mount_id)
        .map_err(show)?;
    if discarded != 0 {
        return Err("clean replacement acquired an old dirty charge".into());
    }
    Ok(())
}

/// Test-only raw transport: a normal `Volume` cannot omit or forge protocol ACKs.
pub(super) fn serve(
    startup: &mut Startup<'_>,
    notice: OwnedHandle<ByteChannelObject>,
    path: &str,
) -> Result<()> {
    let control =
        CapabilityChannel::from_handle(startup.take(filesystem::WORKER_CONTROL).map_err(show)?);
    let _owner = startup.take(filesystem::OWNER).map_err(show)?;
    let (client, server) = hyper_os::channel::create_pair().map_err(show)?;
    let mut buffer = hyper_os::filesystem::SharedBuffer::create(
        startup.borrow(startup::ROOT_VMAR).map_err(show)?,
        protocol::DATA_BYTES,
    )
    .map_err(show)?;
    // Allocate request storage before locking/publishing this worker's mappings.
    let mut request_bytes = vec![0; protocol::REQUEST_BYTES];
    let mut reply = [0; protocol::RESPONSE_BYTES];
    let mut accepted = [0; PAGE];
    hyper_os::memory::lock_current_memory(startup.borrow(startup::ROOT_VMAR).map_err(show)?)
        .map_err(show)?;
    let mut client = Some(client);
    let mut offers = [
        CapabilityDisposition::move_handle(
            &mut client,
            RightsOffer::Exact(filesystem::TRANSPORT_RIGHTS),
        )
        .map_err(show)?,
        CapabilityDisposition::duplicate(
            buffer.memory().as_handle_ref(),
            RightsOffer::Exact(filesystem::BUFFER_RIGHTS),
        )
        .map_err(show)?,
    ];
    hyper_service::io::send_capabilities(
        &control,
        filesystem::WORKER_READY,
        &mut offers,
        deadline()?,
    )
    .map_err(show)?;
    drop(offers);
    let mut submitted = None;
    let mut last_sequence = 0;
    loop {
        wait::wait_many(
            &[WaitItem::new(
                server.as_handle_ref(),
                ObjectSignals::<ByteChannelObject>::READABLE
                    .union(ObjectSignals::<ByteChannelObject>::PEER_CLOSED),
            )],
            hyper_os::DEADLINE_INFINITE,
        )
        .map_err(show)?;
        let length = server
            .as_byte_channel()
            .try_receive(&mut request_bytes)
            .map_err(show)?;
        let request = Request::decode(&request_bytes[..length]).ok_or("invalid fixture request")?;
        if request.sequence <= last_sequence {
            return Err("fixture received replayed request sequence".into());
        }
        last_sequence = request.sequence;
        let mut entry = Entry::empty();
        // SAFETY: This single actor owns the buffered RPC range between request
        // receipt and reply; no slice survives a response or another request.
        let data = unsafe { buffer.bytes_mut() };
        let mut rejection = None;
        let result = match request.operation {
            Operation::Stat => {
                if request.path == "a" {
                    entry.kind = protocol::FILE;
                    entry.size = PAGE as u64;
                    entry.name[0] = b'a';
                    entry.name_len = 1;
                } else if !request.path.is_empty() {
                    return Err("fixture stat addressed an unexpected path".into());
                }
                0
            }
            Operation::Capabilities => protocol::Capabilities::ASYNC_WRITEBACK.bits(),
            Operation::TakeWriteback => 3_600_000_000_000,
            Operation::Read => {
                let start = usize::try_from(request.offset).map_err(show)?.min(PAGE);
                let count = usize::try_from(request.length)
                    .map_err(show)?
                    .min(PAGE - start);
                data[..count].copy_from_slice(&accepted[start..start + count]);
                count as u64
            }
            Operation::SubmitWriteback => {
                if submitted.is_some()
                    || request.path != "a"
                    || request.offset != 0
                    || request.length != PAGE as u64
                    || request.window_token != 0
                    || data[..PAGE].iter().any(|byte| *byte != 0xa5)
                {
                    return Err("fixture received duplicate or invalid writeback input".into());
                }
                accepted.copy_from_slice(&data[..PAGE]);
                submitted = Some(request.sequence);
                notice.as_byte_channel().send(b"accepted").map_err(show)?;
                if path == LOST_ACK {
                    // Keep the provider alive with accepted bytes but no ACK until
                    // the test kills it. A retry would be caught by submitted above.
                    continue;
                }
                PAGE as u64
            }
            Operation::ReapWriteback => {
                let batch = submitted.ok_or("reap without accepted input")?;
                if !matches!(path, BAD_COMPLETION | POST_COMPLETION) || request.offset != batch {
                    return Err("fixture received an unexpected completion request".into());
                }
                protocol::WritebackCompletion {
                    batch: if path == BAD_COMPLETION {
                        batch.checked_add(1).ok_or("fixture batch overflow")?
                    } else {
                        batch
                    },
                    accepted: PAGE as u64,
                    result: Ok(()),
                }
                .encode(&mut data[..protocol::WRITEBACK_COMPLETION_BYTES])
                .ok_or("encode forged completion")?;
                protocol::WRITEBACK_COMPLETION_BYTES as u64
            }
            Operation::SetMetadata if path == POST_COMPLETION && submitted.is_some() => {
                // Bytes completed, but their accompanying timestamp update was
                // rejected without closing this healthy transport. VFS must
                // still fail the mount and retain the exact unretired owners.
                rejection = Some(protocol::Error::NoSpace);
                0
            }
            Operation::Sync | Operation::SyncFile | Operation::SetMetadata => {
                if submitted.is_some() {
                    return Err("failed receipt reached a later filesystem fence".into());
                }
                0
            }
            _ => {
                return Err(format!(
                    "unexpected fixture operation: {:?}",
                    request.operation
                ));
            }
        };
        Response {
            sequence: request.sequence,
            operation: request.operation,
            result: rejection.map_or(Ok(result), Err),
            entry,
        }
        .encode_with_features(&mut reply, protocol::ResponseFeatures::CAPABILITY_QUERY)
        .ok_or("encode fixture response")?;
        server.as_byte_channel().send(&reply).map_err(show)?;
        if request.operation == Operation::ReapWriteback && path == BAD_COMPLETION {
            notice.as_byte_channel().send(b"forged").map_err(show)?;
        }
        if rejection.is_some() {
            notice.as_byte_channel().send(b"rejected").map_err(show)?;
        }
    }
}
