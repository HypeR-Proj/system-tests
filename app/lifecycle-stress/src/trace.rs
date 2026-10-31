// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Bounded real IPC: duplex messages, explicit nested RPC and scheduler waits.

use super::{Result, receive};
use hyper_os::{
    channel::{self, RpcTracer},
    handle::{ByteChannelObject, OwnedHandle},
};

fn request(endpoint: &OwnedHandle<ByteChannelObject>) -> Result<u64> {
    let mut bytes = [0; 8];
    if receive(endpoint, &mut bytes)? != bytes.len() {
        return Err("invalid trace request size".into());
    }
    Ok(u64::from_le_bytes(bytes))
}

pub(super) fn run() -> Result<()> {
    let (client, server) = channel::create_pair()?;
    let (next, leaf) = channel::create_pair()?;
    let leaf = std::thread::Builder::new()
        .name("trace-leaf".into())
        .spawn(move || -> Result<()> {
            let trace = RpcTracer::new(leaf.as_handle_ref());
            for expected in 1..=4 {
                let sequence = request(&leaf)?;
                if sequence != expected {
                    return Err("leaf sequence mismatch".into());
                }
                let context = trace.server_begin(sequence, 7, None);
                std::thread::sleep(std::time::Duration::from_millis(2));
                leaf.as_byte_channel().send(&sequence.to_le_bytes())?;
                trace.server_end(context, 0);
            }
            Ok(())
        })?;
    let server = std::thread::Builder::new()
        .name("trace-server".into())
        .spawn(move || -> Result<()> {
            let trace = RpcTracer::new(server.as_handle_ref());
            let downstream = RpcTracer::new(next.as_handle_ref());
            for expected in 1..=4 {
                let sequence = request(&server)?;
                if sequence != expected {
                    return Err("server sequence mismatch".into());
                }
                let context = trace.server_begin(sequence, 3, None);
                let nested = downstream.client_begin(sequence, 7, context);
                next.as_byte_channel().send(&sequence.to_le_bytes())?;
                if request(&next)? != sequence {
                    return Err("nested response mismatch".into());
                }
                downstream.client_end(nested, 0);
                server.as_byte_channel().send(&sequence.to_le_bytes())?;
                trace.server_end(context, 0);
            }
            Ok(())
        })?;
    let trace = RpcTracer::new(client.as_handle_ref());
    let outcome = (|| -> Result<()> {
        for sequence in 1u64..=4 {
            let context = trace.client_begin(sequence, 3, None);
            client.as_byte_channel().send(&sequence.to_le_bytes())?;
            if request(&client)? != sequence {
                return Err("client response mismatch".into());
            }
            trace.client_end(context, 0);
        }
        Ok(())
    })();
    drop(client); // On failure wake every peer before joining their workers.
    let server_result = server.join().map_err(|_| "trace server panicked")?;
    let leaf_result = leaf.join().map_err(|_| "trace leaf panicked")?;
    outcome?;
    server_result?;
    leaf_result?;
    // On disk-backed systems this also exercises the kernel VFS transport;
    // the ordinary RAM-only Native fixture cannot prove a remote FS path.
    let _contents = std::fs::read("/etc/fstab")?;
    println!("IPC_TRACE_OK requests=4 nested=4");
    Ok(())
}
