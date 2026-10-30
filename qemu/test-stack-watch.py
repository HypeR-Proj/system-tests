#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Validate bounded GDB stack diagnosis without running a VM."""

import importlib.util
from pathlib import Path
import socket
import struct
import time
import unittest

spec = importlib.util.spec_from_file_location('stack_watch', Path(__file__).with_name('watch-stack.py'))
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class Stream:
    def __init__(self, packet):
        self.packet = packet
        self.sent = []

    def recv(self, size):
        # Fragmentation must not produce a short register/frame response.
        size = min(1, size)
        result, self.packet = self.packet[:size], self.packet[size:]
        return result

    def sendall(self, data):
        self.sent.append(data)


class Memory:
    def __init__(self, records):
        self.records = records
        self.reads = []

    def request(self, command):
        self.reads.append(command)
        return self.records[command]


class StackWatchTests(unittest.TestCase):
    def test_remote_checksum_fragmentation_and_request_acknowledgement(self):
        stream = Stream(b'+$OK#9a')
        self.assertEqual(watch.Remote(stream).request('?'), 'OK')
        self.assertEqual(stream.sent, [b'$?#3f', b'+'])

    def test_remote_disconnect_and_bad_checksum_fail(self):
        for packet, error in [(b'+$O', EOFError), (b'$OK#00', ValueError)]:
            with self.subTest(packet=packet), self.assertRaises(error):
                watch.Remote(Stream(packet)).receive()

    def test_continue_ack_does_not_hide_completion_or_deadline(self):
        class Suspended(Stream):
            def recv(self, size):
                if not self.packet:
                    raise socket.timeout()
                return super().recv(size)

            def sendall(self, data):
                super().sendall(data)
                if data == b'\x03':
                    payload = b'T05thread:01;'
                    self.packet += b'$' + payload + b'#' + f'{sum(payload) % 256:02x}'.encode()

            def gettimeout(self):
                return 5

            def settimeout(self, timeout):
                self.timeout = timeout

        for complete in [True, False]:
            stream = Suspended(b'+')
            deadline = time.monotonic() + (10 if complete else -1)
            self.assertEqual(watch.Remote(stream).resume(deadline, lambda: complete),
                             'T05thread:01;')
            self.assertIn(b'\x03', stream.sent)
            self.assertEqual(stream.timeout, 5)

    def test_stack_geometry_rejects_guards_unused_tails_and_other_regions(self):
        bottom = watch.ARENA + 20 * watch.STRIDE + watch.GUARD
        self.assertEqual(watch.stack_bounds(bottom + 2000), (bottom, bottom + watch.STACK_SIZE))
        for address in [0, bottom - 1, bottom + watch.STACK_SIZE,
                        watch.ARENA + watch.SLOT_COUNT * watch.STRIDE + watch.GUARD]:
            self.assertIsNone(watch.stack_bounds(address))

    def test_booted_layout_must_match_configured_geometry(self):
        watch.check_boot_layout(b'guarded kernel stacks: thread 16 KiB,\n'
                                b'AArch64 address space: 48-bit VA')
        for record in [b'guarded kernel stacks: thread 32 KiB,',
                       b'AArch64 address space: 42-bit VA']:
            with self.subTest(record=record), self.assertRaises(ValueError):
                watch.check_boot_layout(record)

    def test_unwind_never_follows_outside_the_stopped_stack(self):
        memory = Memory({'m1020,10': struct.pack('<QQ', 0x1040, 0x9000).hex(),
                         'm1040,10': struct.pack('<QQ', 0xffff0000, 0xa000).hex()})
        frames = watch.unwind(memory, 0x1020, 0x1000, 0x1100)
        self.assertEqual([f['return_address'] for f in frames], [0x9000, 0xa000])
        self.assertEqual(memory.reads, ['m1020,10', 'm1040,10'])

    def test_unwind_rejects_misalignment_cycles_and_short_reads(self):
        memory = Memory({'m1020,10': struct.pack('<QQ', 0x1020, 0x9000).hex()})
        self.assertEqual(watch.unwind(memory, 0x1021, 0x1000, 0x1100), [])
        self.assertEqual(len(watch.unwind(memory, 0x1020, 0x1000, 0x1100)), 1)
        with self.assertRaisesRegex(ValueError, 'short'):
            watch.unwind(Memory({'m1020,10': '00'}), 0x1020, 0x1000, 0x1100)

    def test_symbolization_accounts_for_kaslr_and_return_instructions(self):
        captures = [{'pc': 0x1100, 'frames': [{'return_address': 0x1200}]}]
        watch.symbolize(captures, [(0x100, 'current'), (0x1f0, 'caller'), (0x200, 'next')], 0x1000)
        self.assertEqual(captures[0]['instruction']['symbol'], 'current')
        self.assertEqual(captures[0]['frames'][0]['symbol'], 'caller')
        self.assertEqual(captures[0]['frames'][0]['symbol_offset'], 12)


if __name__ == '__main__':
    unittest.main()
