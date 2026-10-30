// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

use super::*;
use hyper_os::{
    Error, Status, channel,
    handle::{ByteChannelObject, OwnedHandle},
    startup::StartupPurpose,
    wait::{ObjectSignals, WaitItem, wait_many},
};
pub(super) const CONTROL: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x80fa_0001);
pub(super) const PEER: StartupPurpose<ByteChannelObject> = StartupPurpose::new(0x80fa_0002);
pub(super) const READY: u8 = 1;
pub(super) const DONE: u8 = 2;
pub(super) const DATA: u8 = 3;
pub(super) const END: u8 = 4;
pub(super) const FAILURE: u8 = 5;
pub(super) const CHUNK: usize = 1024;
pub(super) const MAX_REPORT: usize = 256 * 1024 * 1024;

pub(super) struct Channel(pub OwnedHandle<ByteChannelObject>);
impl Channel {
    pub(super) fn send(&self, data: &[u8], deadline: u64) -> Result<()> {
        loop {
            match self.0.as_byte_channel().try_send(data) {
                Ok(()) => return Ok(()),
                Err(Error::Status(Status::WOULD_BLOCK)) => {
                    wait_many(
                        &[WaitItem::new(
                            self.0.as_handle_ref(),
                            ObjectSignals::<ByteChannelObject>::WRITABLE
                                .union(ObjectSignals::<ByteChannelObject>::PEER_CLOSED),
                        )],
                        deadline,
                    )
                    .map_err(|e| format!("send wait: {e:?}"))?;
                }
                Err(e) => return Err(format!("channel send: {e:?}")),
            }
            if now()? >= deadline {
                return Err("channel send timed out".into());
            }
        }
    }
    pub(super) fn receive(&self, output: &mut [u8], deadline: u64) -> Result<usize> {
        loop {
            match self.0.as_byte_channel().try_receive(output) {
                Ok(n) => return Ok(n),
                Err(Error::Status(Status::WOULD_BLOCK)) => {
                    wait_many(
                        &[WaitItem::new(
                            self.0.as_handle_ref(),
                            ObjectSignals::<ByteChannelObject>::READABLE
                                .union(ObjectSignals::<ByteChannelObject>::PEER_CLOSED),
                        )],
                        deadline,
                    )
                    .map_err(|e| format!("receive wait: {e:?}"))?;
                }
                Err(e) => return Err(format!("channel receive: {e:?}")),
            }
            if now()? >= deadline {
                return Err("channel receive timed out".into());
            }
        }
    }
    pub(super) fn marker(&self, tag: u8, deadline: u64) -> Result<()> {
        self.send(&[tag], deadline)
    }
    pub(super) fn expect(&self, expected: u8, deadline: u64) -> Result<()> {
        let mut buf = [0; CHUNK];
        let n = self.receive(&mut buf, deadline)?;
        if buf[..n] == [expected] {
            Ok(())
        } else if buf.first() == Some(&FAILURE) {
            Err(String::from_utf8_lossy(&buf[1..n]).into_owned())
        } else {
            Err(format!(
                "unexpected worker frame, expected {expected}, length={n}"
            ))
        }
    }
}
pub(super) fn pair() -> Result<(Channel, OwnedHandle<ByteChannelObject>)> {
    let (a, b) = channel::create_pair().map_err(|e| format!("create channel: {e:?}"))?;
    Ok((Channel(a), b))
}
