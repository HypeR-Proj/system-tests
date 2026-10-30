// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Opt-in probe executed by the exclusive FAT worker before mounting its volume.
//! Writes preserve existing sector bytes; normal filesystem acceptance tests
//! subsequently exercise changed payloads and cold-restart durability.

use hyper_os::block::{
    BlockQueue, NativeBlock, QueueConfig, ReadRequest, WindowWrite, WriteRequest,
};
use hyper_os::handle::{HandleRef, Rights, VmarObject};
use hyper_os::{Error, Status};
use std::time::Duration;

fn show(error: impl core::fmt::Debug) -> String {
    format!("block queue probe: {error:?}")
}

fn require(condition: bool, message: &str) -> Result<(), String> {
    if condition {
        Ok(())
    } else {
        Err(format!("block queue probe: {message}"))
    }
}

fn deadline() -> Result<u64, String> {
    hyper_os::time::deadline_after(Duration::from_secs(30))
        .map(|value| value.as_raw())
        .map_err(show)
}

fn wait(queue: &BlockQueue, ticket: hyper_os::block::Ticket) -> Result<(), String> {
    queue
        .wait(ticket, deadline()?)
        .map_err(show)?
        .result()
        .map_err(show)
}

// Every error aborts this probe and drops its queues. Production callers keep
// each ticket until reap, including when a wait is cancelled or times out.
fn read(queue: &BlockQueue, first: u64, output: &mut [u8]) -> Result<(), String> {
    let ticket = queue.submit_read(first, output.len()).map_err(show)?;
    wait(queue, ticket)?;
    queue
        .reap(ticket, output)
        .map_err(show)?
        .result()
        .map_err(show)
}

fn flush(queue: &BlockQueue) -> Result<(), String> {
    let ticket = queue.submit_flush().map_err(show)?;
    wait(queue, ticket)?;
    queue
        .reap(ticket, &mut [])
        .map_err(show)?
        .result()
        .map_err(show)
}

fn admission_wait(block: &NativeBlock) -> Result<(), String> {
    // Retain exactly the public device byte budget. The remaining kernel-only
    // credit must still support paging while a producer copies or reaps data.
    let owner = block
        .create_queue(QueueConfig {
            capacity: 32,
            byte_limit: 8 * 1024 * 1024,
            inflight_limit: 4,
        })
        .map_err(show)?;
    let consumer = block.create_queue(QueueConfig::default()).map_err(show)?;
    let mut held = Vec::new();
    for length in std::iter::repeat_n(512 * 1024, 14).chain(std::iter::repeat_n(64 * 1024, 8)) {
        let ticket = owner.submit_read(0, length).map_err(show)?;
        wait(&owner, ticket)?;
        held.push((ticket, length));
    }
    require(
        matches!(
            consumer.submit_read(0, 64 * 1024),
            Err(Error::Status(Status::WOULD_BLOCK))
        ),
        "public queues consumed reserved device credits",
    )?;
    require(
        matches!(
            consumer.wait_space(64 * 1024, 0),
            Err(Error::Status(Status::TIMED_OUT))
        ),
        "space wait ignored retained global payloads",
    )?;
    let mut output = vec![0; 512 * 1024];
    block.read_sectors(0, &mut output).map_err(show)?;
    let (release, length) = held
        .pop()
        .ok_or_else(|| String::from("block queue probe: missing held payload"))?;
    let limit = deadline()?;
    std::thread::scope(|scope| -> Result<(), String> {
        let waiting = scope.spawn(|| consumer.wait_space(length, limit));
        owner
            .reap(release, &mut output[..length])
            .map_err(show)?
            .result()
            .map_err(show)?;
        waiting
            .join()
            .map_err(|_| String::from("block queue probe: space waiter panicked"))?
            .map_err(show)?;
        Ok(())
    })?;
    // Only 64 KiB is now available: the maximum-size WRITABLE level remains
    // clear, but this exact-size wait and submission must make progress.
    require(
        matches!(
            consumer.wait_space(512 * 1024, 0),
            Err(Error::Status(Status::TIMED_OUT))
        ),
        "space wait used the wrong requested size",
    )?;
    consumer.wait_space(length, 0).map_err(show)?;
    read(&consumer, 0, &mut output[..length])?;
    for (ticket, length) in held {
        owner
            .reap(ticket, &mut output[..length])
            .map_err(show)?
            .result()
            .map_err(show)?;
    }
    Ok(())
}

fn readonly_window(block: &NativeBlock, root: HandleRef<'_, VmarObject>) -> Result<(), String> {
    use hyper_os::filesystem::SharedBuffer;
    let queue = block.create_queue(QueueConfig::default()).map_err(show)?;
    let window = queue.create_window().map_err(show)?;
    let rights = window.info().map_err(show)?.rights;
    require(
        rights.contains(Rights::READ.union(Rights::MAP)) && !rights.contains(Rights::WRITE),
        "window exported write authority",
    )?;
    require(
        window.duplicate(rights.union(Rights::WRITE)).is_err(),
        "window duplicate escalated write authority",
    )?;
    let second = queue.create_window().map_err(show)?;
    require(
        matches!(
            SharedBuffer::from_memory(root, second, 512 * 1024),
            Err(Error::Status(Status::ACCESS_DENIED))
        ),
        "window accepted writable mapping",
    )?;
    let mapping = SharedBuffer::from_readonly_memory(root, window, 512 * 1024).map_err(show)?;
    let before = queue.info().map_err(show)?;
    for token in [1, u64::MAX] {
        require(
            matches!(
                queue.submit_window(
                    token,
                    &[WindowWrite {
                        first: 0,
                        offset: 0,
                        bytes: 512
                    }]
                ),
                Err(Error::Status(Status::INVALID_ARGUMENT))
            ),
            "unissued window token admitted I/O",
        )?;
    }
    let after = queue.info().map_err(show)?;
    require(
        before.submitted_ranges == after.submitted_ranges && after.outstanding_requests == 0,
        "invalid token changed queue admission",
    )?;
    // No filesystem binds this window, so no producer can modify its zeroed
    // contents. A retained read mapping must outlive its original queue handle.
    drop(queue);
    require(
        unsafe { mapping.bytes() }.iter().all(|byte| *byte == 0),
        "closed queue invalidated readonly backing",
    )
}

pub(super) fn run(
    block: &NativeBlock,
    sectors: u64,
    readonly: bool,
    root: HandleRef<'_, VmarObject>,
) -> Result<(), String> {
    require(sectors >= 1024, "fixture volume too small")?;
    readonly_window(block, root)?;
    admission_wait(block)?;
    let queue = block
        .create_queue(QueueConfig {
            capacity: 8,
            byte_limit: 1024 * 1024,
            inflight_limit: 4,
        })
        .map_err(show)?;
    let mut baseline = vec![0; 16 * 512];
    read(&queue, 0, &mut baseline)?;

    // One atomic batch presents all adjacent ranges together to the real
    // scheduler, making merge acceptance independent of worker wake timing.
    let before = queue.info().map_err(show)?;
    let ranges: Vec<_> = (0..16)
        .map(|first| ReadRequest { first, length: 512 })
        .collect();
    let ticket = queue.submit_reads(&ranges).map_err(show)?;
    wait(&queue, ticket)?;
    require(
        queue.wait_any(0).map_err(show)?.ticket == ticket,
        "wait-any lost completion",
    )?;
    // An invalid reap must leave the result available to a later valid reap.
    require(
        queue.reap(ticket, &mut [0; 512]).is_err(),
        "short reap consumed read",
    )?;
    wait(&queue, ticket)?;
    let mut readback = vec![0; baseline.len()];
    let completion = queue.reap(ticket, &mut readback).map_err(show)?;
    completion.result().map_err(show)?;
    require(
        completion.bytes == baseline.len() && readback == baseline,
        "scatter read differs",
    )?;
    require(
        queue.wait(ticket, 0).is_err(),
        "reaped ticket remained live",
    )?;
    let after = queue.info().map_err(show)?;
    require(
        after.submitted_ranges - before.submitted_ranges == 16,
        "range counter differs",
    )?;
    require(
        after.dispatched_commands - before.dispatched_commands == 1,
        "adjacent reads not merged",
    )?;
    require(
        after.merged_ranges - before.merged_ranges == 15,
        "merge counter differs",
    )?;

    // Disjoint, nonadjacent ranges cannot merge. One admitted batch lets the
    // pump fill all four heads before collecting, independent of submit timing.
    let disjoint = [0, 2, 4, 6].map(|first| ReadRequest { first, length: 512 });
    let ticket = queue.submit_reads(&disjoint).map_err(show)?;
    wait(&queue, ticket)?;
    let mut scattered = [0; 4 * 512];
    queue
        .reap(ticket, &mut scattered)
        .map_err(show)?
        .result()
        .map_err(show)?;
    for (index, bytes) in scattered.chunks_exact(512).enumerate() {
        require(
            bytes == &baseline[index * 1024..index * 1024 + 512],
            "concurrent read returned another command's payload",
        )?;
    }
    let concurrent = queue.info().map_err(show)?;
    require(
        concurrent.dispatched_commands - after.dispatched_commands == 4
            && concurrent.peak_inflight == 4,
        "independent commands did not fill device depth",
    )?;

    // Independent observers may both wait, but only one caller can collect a
    // result. A concurrently observed completion must survive another waiter.
    let observed = queue.submit_read(0, 512).map_err(show)?;
    std::thread::scope(|scope| -> Result<(), String> {
        let first = scope.spawn(|| wait(&queue, observed));
        let second = scope.spawn(|| wait(&queue, observed));
        first
            .join()
            .map_err(|_| String::from("block queue probe: first waiter panicked"))??;
        second
            .join()
            .map_err(|_| String::from("block queue probe: second waiter panicked"))??;
        Ok(())
    })?;
    queue
        .reap(observed, &mut [0; 512])
        .map_err(show)?
        .result()
        .map_err(show)?;

    // Completed, uncollected payloads still own both request and byte credits.
    let bounded = block
        .create_queue(QueueConfig {
            capacity: 2,
            byte_limit: 1024,
            inflight_limit: 1,
        })
        .map_err(show)?;
    let first = bounded.submit_read(0, 512).map_err(show)?;
    let second = bounded.submit_read(1, 512).map_err(show)?;
    wait(&bounded, first)?;
    wait(&bounded, second)?;
    require(
        matches!(
            bounded.submit_read(2, 512),
            Err(Error::Status(Status::WOULD_BLOCK))
        ),
        "completed data escaped admission budget",
    )?;
    let info = bounded.info().map_err(show)?;
    require(
        info.outstanding_requests == 2
            && info.outstanding_bytes == 1024
            && info.completed_requests == 2,
        "completion occupancy differs",
    )?;
    let mut sector = [0; 512];
    bounded
        .reap(second, &mut sector)
        .map_err(show)?
        .result()
        .map_err(show)?;
    let third = bounded.submit_read(2, 512).map_err(show)?;
    require(third != second, "live slot reused its ticket generation")?;
    wait(&bounded, third)?;
    bounded
        .reap(first, &mut sector)
        .map_err(show)?
        .result()
        .map_err(show)?;
    bounded
        .reap(third, &mut sector)
        .map_err(show)?
        .result()
        .map_err(show)?;
    require(
        bounded.info().map_err(show)?.outstanding_bytes == 0,
        "reap did not release credit",
    )?;
    drop(bounded);

    // The kernel validates volume bounds independently of SDK frame geometry.
    let submitted = queue.info().map_err(show)?.submitted_batches;
    require(
        queue.submit_read(sectors, 512).is_err(),
        "out-of-volume read admitted",
    )?;
    require(
        queue.info().map_err(show)?.submitted_batches == submitted,
        "failed submit had effects",
    )?;

    if !readonly {
        let requests: Vec<_> = baseline
            .chunks(512)
            .enumerate()
            .map(|(index, bytes)| WriteRequest {
                first: index as u64,
                bytes,
            })
            .collect();
        let write = queue.submit_writes(&requests).map_err(show)?;
        let fence = queue.submit_flush().map_err(show)?;
        let read_ticket = queue.submit_read(0, baseline.len()).map_err(show)?;
        // Wait for the last command first: a synchronous per-ticket queue
        // would deadlock here if callers had to reap before later progress.
        wait(&queue, read_ticket)?;
        wait(&queue, fence)?;
        wait(&queue, write)?;
        queue
            .reap(write, &mut [])
            .map_err(show)?
            .result()
            .map_err(show)?;
        queue
            .reap(fence, &mut [])
            .map_err(show)?
            .result()
            .map_err(show)?;
        queue
            .reap(read_ticket, &mut readback)
            .map_err(show)?
            .result()
            .map_err(show)?;
        require(
            readback == baseline,
            "write/fence/read damaged sector contents",
        )?;

        // Four full filesystem windows can share a single physical command;
        // the transport must reserve the entire span until its completion.
        let mut large = vec![0; 512 * 1024];
        read(&queue, 0, &mut large)?;
        let before = queue.info().map_err(show)?;
        let windows: Vec<_> = large
            .chunks(128 * 1024)
            .enumerate()
            .map(|(index, bytes)| WriteRequest {
                first: (index * 256) as u64,
                bytes,
            })
            .collect();
        let write = queue.submit_writes(&windows).map_err(show)?;
        wait(&queue, write)?;
        queue
            .reap(write, &mut [])
            .map_err(show)?
            .result()
            .map_err(show)?;
        let after = queue.info().map_err(show)?;
        require(
            after.dispatched_commands - before.dispatched_commands == 1,
            "full adjacent windows not merged",
        )?;
        flush(&queue)?;
        let mut verify = vec![0; large.len()];
        read(&queue, 0, &mut verify)?;
        require(
            verify == large,
            "multi-slot command damaged sector contents",
        )?;
    }

    let restricted = block.create_queue(QueueConfig::default()).map_err(show)?;
    let restricted = BlockQueue::from_handle(
        restricted
            .into_handle()
            .replace(Rights::READ.union(Rights::WAIT).union(Rights::INSPECT))
            .map_err(|_| String::from("block queue probe: cannot attenuate queue"))?,
    );
    require(
        matches!(
            restricted.submit_write(0, &baseline[..512]),
            Err(Error::Status(Status::ACCESS_DENIED))
        ),
        "read-only queue submitted a write",
    )?;
    read(&restricted, 0, &mut sector)?;
    drop(restricted);

    let wait_only = block.create_queue(QueueConfig::default()).map_err(show)?;
    let hidden = wait_only.submit_read(0, 512).map_err(show)?;
    let wait_only = BlockQueue::from_handle(
        wait_only
            .into_handle()
            .replace(Rights::WAIT)
            .map_err(|_| String::from("block queue probe: cannot create wait-only capability"))?,
    );
    wait(&wait_only, hidden)?;
    require(
        matches!(
            wait_only.reap(hidden, &mut sector),
            Err(Error::Status(Status::ACCESS_DENIED))
        ),
        "WAIT granted access to read data",
    )?;
    // Failed authorization cannot consume or damage completion state.
    wait(&wait_only, hidden)?;
    drop(wait_only);

    // Closing an independent queue cannot close the volume or another queue.
    let abandoned = block.create_queue(QueueConfig::default()).map_err(show)?;
    let _pending = abandoned.submit_read(0, 512 * 1024).map_err(show)?;
    drop(abandoned);
    read(&queue, 0, &mut sector)?;
    require(sector == baseline[..512], "queue close poisoned sibling")?;
    let info = queue.info().map_err(show)?;
    require(
        info.outstanding_requests == 0 && info.outstanding_bytes == 0,
        "probe left unreaped completions",
    )?;
    eprintln!(
        "BLOCK-QUEUE: PASS ranges={} commands={} merged={} peak={}",
        info.submitted_ranges, info.dispatched_commands, info.merged_ranges, info.peak_inflight
    );
    Ok(())
}
