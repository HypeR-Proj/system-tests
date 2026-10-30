#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Power-boundary evidence survives independent diagnostics and console delivery."""

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

from hyper_test.session import Session


spec = importlib.util.spec_from_file_location(
    'power_crash', Path(__file__).with_name('verify-power-crash.py'))
power_crash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(power_crash)


class PowerCrashTests(unittest.TestCase):
    def test_online_reply_survives_interleaved_printk_at_every_read_boundary(self):
        warning = b'[    7.964368] hrtimer: interrupt took 6024560 ns\r\n'
        output = b'\r\nPOWER_CRASH_ONLINE=' + warning + b'0-3\r\n~ # \x1b[6n'
        expected = b'\nPOWER_CRASH_ONLINE=0-3\n~ # \x1b[6n'
        for split in range(len(output) + 1):
            with self.subTest(split=split):
                pending = bytearray()
                for chunk in (output[:split], output[split:]):
                    power_crash.CONSOLE_OUTPUT(pending, chunk)
                self.assertEqual(pending, expected)
        pending = bytearray()
        for byte in output:
            power_crash.CONSOLE_OUTPUT(pending, bytes([byte]))
        self.assertEqual(pending, expected)

    def test_console_filter_keeps_wrong_cpu_mask_and_rejects_guest_failure(self):
        pending = bytearray()
        power_crash.CONSOLE_OUTPUT(
            pending, b'\nPOWER_CRASH_ONLINE=[ 1.0] diagnostic\n0-2\n')
        self.assertEqual(pending, b'\nPOWER_CRASH_ONLINE=0-2\n')
        for failure in (b'Kernel panic', b'watchdog: BUG: soft lockup'):
            with self.subTest(failure=failure), self.assertRaisesRegex(
                    RuntimeError, 'kernel failure'):
                power_crash.CONSOLE_OUTPUT(bytearray(), b'[ 1.0] ' + failure + b'\n')

    def test_marker_and_disconnect_may_arrive_in_either_order(self):
        marker = b'HYPER_POWER_CRASH state=powered-off\r\n'
        disconnect = b'[vmm] virtual machine disconnected\r\n'
        prompt = b'hyper-sh$ '
        for output in (marker + disconnect + prompt,
                       disconnect + marker + prompt,
                       disconnect + prompt + marker):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                script = (
                    'import sys,time; '
                    f'sys.stdout.buffer.write({marker!r}); sys.stdout.flush(); '
                    'sys.stdin.buffer.readline(); '
                    f'sys.stdout.buffer.write({output!r}); sys.stdout.flush(); '
                    'time.sleep(30)')
                with Session([sys.executable, '-u', '-c', script],
                             Path(directory) / 'serial.log') as session:
                    session.await_text(marker.replace(b'\r', b''), timeout=2)
                    boundary = session.mark()
                    session.send(b'crash\n')
                    power_crash.await_power_loss(session, 'powered-off', boundary)
                self.assertIsNotNone(session.process.returncode)


if __name__ == '__main__':
    unittest.main()
