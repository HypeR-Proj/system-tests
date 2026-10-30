// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

#[test]
fn rejects_invalid_guest_blob_ranges() {
    assert!(!super::validate_payload(&[]));
    assert!(!super::validate_payload(&[0]));
    assert!(super::validate_payload(&[1, 0]));
    assert!(!super::validate_payload(&vec![
        0;
        super::RAM_BYTES as usize + 2
    ]));
}

#[test]
fn protocol_does_not_hide_extra_or_failure_output() {
    assert!(super::consume_marker(b"T", b'T'));
    assert!(!super::consume_marker(b"TF", b'T'));
    assert!(!super::consume_marker(b"F", b'T'));
    assert!(!super::consume_marker(b"", b'T'));
}
