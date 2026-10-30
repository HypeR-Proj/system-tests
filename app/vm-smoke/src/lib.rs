// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Small validation helpers shared by the privileged VM integration fixture.

pub const RAM_BASE: u64 = 0x8000_0000;
pub const RAM_BYTES: u64 = 2 * 1024 * 1024;
pub const BOOT_SENTINELS: [u64; 2] = [0x1234, 0x5678];

pub fn validate_payload(bytes: &[u8]) -> bool {
    !bytes.is_empty() && bytes.len() <= RAM_BYTES as usize && bytes.len().is_multiple_of(2)
}

/// Output is a deliberately tiny protocol; an unexpected byte is a guest failure.
pub fn consume_marker(bytes: &[u8], expected: u8) -> bool {
    bytes == [expected]
}

#[cfg(test)]
#[path = "../tests/fixture.rs"]
mod tests;
