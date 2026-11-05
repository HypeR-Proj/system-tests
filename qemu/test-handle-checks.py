#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Inspection must not mistake per-command handles for retained capabilities."""
import unittest

from handle_checks import stable_handle


def row(handle, obj, rights='read|wait|transfer'):
    return f'\n0x{handle:016x} 0x{obj:016x} byte-channel {rights}\n'.encode()


class StableHandleTests(unittest.TestCase):
    def test_skips_foreground_jobs_with_identical_rights(self):
        persistent = row(10, 20)
        first = row(30, 40) + persistent
        second = row(50, 60) + persistent
        self.assertEqual(stable_handle(first, second, 'byte-channel')[:2],
                         (b'0x000000000000000a', b'0x0000000000000014'))

    def test_requires_handle_and_object_identity_and_rights(self):
        for replacement in (row(11, 20), row(10, 21), row(10, 20, 'read|wait')):
            with self.subTest(replacement=replacement), self.assertRaises(AssertionError):
                stable_handle(row(10, 20), replacement, 'byte-channel')


if __name__ == '__main__':
    unittest.main()
