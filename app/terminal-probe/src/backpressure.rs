// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Fill a private response queue, cancel its blocked read, then drain in order.

use hyper_os::stream::{self, Header};
use hyper_terminal::{Settings, Terminal};

use crate::{Result, require};

pub(super) fn run(terminal: &mut Terminal) -> Result<()> {
    terminal.set_settings(Settings::raw())?;
    let input = hyper_rt::process::stdin_stream()?.ok_or("missing input connector")?;
    let connection = stream::connect(input.as_handle_ref(), stream::ACCESS_READ)?;
    let channel = connection.as_byte_channel();
    // ByteChannel holds sixteen messages. Seventeen expired reads leave the
    // last completion blocked without consuming the sixteen queued replies.
    for id in 1..=17 {
        channel.send(&Header::request(stream::READ, id, 1, 0).encode())?;
    }
    channel.send(&Header::request(stream::CANCEL, 17, 17, 0).encode())?;
    // The queued next request must not overwrite the cancellation reply.
    channel.send(&Header::request(stream::READ, 18, 1, 0).encode())?;
    let output = hyper_rt::process::stdout_stream()?.ok_or("missing output connector")?;
    let mut writer = stream::connect(output.as_handle_ref(), stream::ACCESS_WRITE)?;
    // A separate data stream and control remain usable. Serial fences exercise
    // at least eighteen stream-service turns before draining this response
    // queue, so cancellation reaches the full queue without a timed sleep.
    for _ in 0..18 {
        writer.fence()?;
        terminal.size()?;
    }
    for id in 1..=18 {
        let mut bytes = [0; stream::HEADER_BYTES];
        let count = channel.receive(&mut bytes)?;
        require(count == bytes.len(), "backpressured reply length")?;
        let reply = Header::decode(&bytes)?;
        reply.status()?;
        require(
            reply.operation == stream::READ | stream::RESPONSE
                && reply.id == id
                && reply.value == 0
                && reply.flags
                    == if id == 17 {
                        stream::CANCELLED
                    } else {
                        stream::TIMED_OUT
                    },
            "backpressured reply order and cancellation",
        )?;
    }
    require(
        matches!(
            channel.try_receive(&mut [0; stream::HEADER_BYTES]),
            Err(hyper_os::Error::Status(hyper_os::Status::WOULD_BLOCK))
        ),
        "cancellation must not duplicate a committed reply",
    )?;
    Ok(())
}
