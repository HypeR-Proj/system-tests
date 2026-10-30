#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for guest SMP placement and multiplexed console output."""

import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    'guest_smp', Path(__file__).with_name('verify-guest-smp.py'))
guest_smp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest_smp)


class FailureObservationTests(unittest.TestCase):
    def session(self):
        return Mock(failures=(b'Kernel panic',), output_filter=guest_smp.append_console_output,
                    pending=bytearray(b'old hyper-sh$ prompt'))

    def test_unresponsive_host_does_not_replace_failure_or_leave_checks_disabled(self):
        session = self.session()
        session.await_text.side_effect = TimeoutError('host unavailable')
        with patch.object(guest_smp, 'Shell') as shell, patch('sys.stderr', io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'original guest failure'):
                try:
                    raise RuntimeError('original guest failure')
                except RuntimeError:
                    guest_smp.capture_failure_state(session, True)
                    raise
        shell.assert_not_called()
        session.send.assert_called_once_with(b'\x1dd')
        session.await_text.assert_called_once_with(rb'hyper-sh\$ ', timeout=10)
        self.assertEqual(session.pending, b'')
        self.assertEqual(session.failures, (b'Kernel panic',))
        self.assertIs(session.output_filter, guest_smp.append_console_output)

    def test_capture_reads_host_state_after_detach_without_restarting_guest(self):
        session = self.session()
        output = io.StringIO()
        with patch.object(guest_smp, 'Shell') as shell, patch('sys.stderr', output):
            shell.return_value.run.return_value = Mock(status=0, output=b'observed\n')
            guest_smp.capture_failure_state(session, True)
        self.assertEqual([call.args[0] for call in shell.return_value.run.call_args_list],
                         ['vmm status alpine', 'ps --threads', 'top -b -n 2',
                          'journalctl -n 60'])
        self.assertTrue(all(call.kwargs == {'timeout': 10}
                            for call in shell.return_value.run.call_args_list))
        self.assertEqual(output.getvalue().count('observed\n'), 4)
        session.send.assert_called_once_with(b'\x1dd')
        self.assertIs(session.output_filter, guest_smp.append_console_output)

    def test_failed_observation_stops_collection_and_restores_checks(self):
        session = self.session()
        with patch.object(guest_smp, 'Shell') as shell, patch('sys.stderr', io.StringIO()):
            shell.return_value.run.side_effect = TimeoutError('command unavailable')
            guest_smp.capture_failure_state(session, False)
        session.send.assert_called_once_with(b'\x03\n')
        shell.return_value.run.assert_called_once_with('vmm status alpine', timeout=10)
        self.assertEqual(session.failures, (b'Kernel panic',))
        self.assertIs(session.output_filter, guest_smp.append_console_output)


class HostPlacementTests(unittest.TestCase):
    def startup(self, placements, *, slot=0, generation=0):
        return b''.join(
            (f'HypeR: vCPU {cpu} running as scheduler thread {cpu + 1} '
             f'on guarded stack 0x1000-0x2000; host CPU {host}; '
             f'VM VmId {{ slot: {slot}, generation: {generation} }}\n').encode()
            for cpu, host in placements.items())

    def status(self, placements):
        return b''.join(f'  vCPU {cpu}: pCPU {host}\n'.encode()
                        for cpu, host in placements.items())

    def test_startup_may_reuse_hosts_between_first_dispatches(self):
        guest_smp.verify_vcpu_startup(self.startup({0: 2, 1: 0, 2: 1, 3: 2}), 4, 4)

    def test_single_host_runs_every_guest_cpu(self):
        guest_smp.verify_vcpu_startup(self.startup(dict.fromkeys(range(4), 0)), 4, 1)

    def test_startup_rejects_unavailable_host(self):
        with self.assertRaisesRegex(RuntimeError, 'invalid host CPU'):
            guest_smp.verify_vcpu_startup(self.startup({0: 0, 1: 4}), 2, 4)

    def test_startup_requires_every_guest_cpu(self):
        for placements in ({}, {0: 0}, {0: 0, 2: 1}):
            with self.subTest(placements=placements), self.assertRaisesRegex(
                    RuntimeError, 'missing complete'):
                guest_smp.verify_vcpu_startup(self.startup(placements), 2, 4)

    def test_different_vm_incarnations_cannot_complete_startup(self):
        for identity in ({'slot': 1}, {'generation': 1}):
            log = self.startup({0: 0}) + self.startup({1: 1}, **identity)
            with self.subTest(identity=identity), self.assertRaisesRegex(
                    RuntimeError, 'missing complete'):
                guest_smp.verify_vcpu_startup(log, 2, 4)

    def test_live_status_accepts_exact_spread_and_colocation(self):
        for placements in ({0: 1, 1: 2, 2: 3, 3: 0}, dict.fromkeys(range(4), 0)):
            with self.subTest(placements=placements):
                guest_smp.verify_host_affinity(self.status(placements), placements)

    def test_live_status_requires_complete_exact_affinity(self):
        targets = {0: 1, 1: 0}
        cases = [self.status(placements) for placements in (
            {}, {0: 1}, {0: 1, 1: 0, 2: 2}, {0: 0, 1: 1}, {0: 0, 1: 0},
            {0: 1, 1: 4}, {0: 1, 1: 'unassigned'}, {0: 1, 1: '0 pending'})]
        cases.append(self.status(targets) + self.status({0: 1}))
        for status in cases:
            with self.subTest(status=status), self.assertRaisesRegex(
                    RuntimeError, 'vCPU affinity mismatch'):
                guest_smp.verify_host_affinity(status, targets)


class ConsoleOutputTests(unittest.TestCase):
    warning = (b'<4>[   66.643508] HypeR: masked virtual timer PPI '
               b'without an active vCPU\r\n')

    def normalize(self, chunks):
        pending = bytearray()
        for chunk in chunks:
            guest_smp.append_console_output(pending, chunk)
        return pending

    def test_ci_failure_prompt_survives_every_read_boundary(self):
        data = b'~' + self.warning + b' # \x1b[6n'
        for split in range(len(data) + 1):
            with self.subTest(split=split):
                self.assertEqual(self.normalize((data[:split], data[split:])), b'~ # \x1b[6n')
        self.assertEqual(self.normalize(bytes([byte]) for byte in data), b'~ # \x1b[6n')

    def test_host_prompt_and_guest_result_rejoin_across_multiple_logs(self):
        data = b'hyper-' + self.warning + b'sh$ \r\nSMP_ON' + self.warning + b'LINE=0-3\r\n'
        self.assertEqual(self.normalize((data,)), b'hyper-sh$ \nSMP_ONLINE=0-3\n')

    def test_guest_printk_interleaved_with_cpu_result(self):
        warning = b'[   60.431869] hrtimer: interrupt took 6591392 ns\r\n'
        data = b'\r\nSMP_MIGRATED_1=' + warning + b'0-3\r\n~ # '
        for split in range(len(data) + 1):
            with self.subTest(split=split):
                pending = bytearray()
                for chunk in (data[:split], data[split:]):
                    guest_smp.append_console_output(pending, chunk, filter_guest_logs=True)
                self.assertEqual(pending, b'\nSMP_MIGRATED_1=0-3\n~ # ')
        pending = bytearray()
        for byte in data:
            guest_smp.append_console_output(pending, bytes([byte]), filter_guest_logs=True)
        self.assertEqual(pending, b'\nSMP_MIGRATED_1=0-3\n~ # ')

    def test_guest_filter_preserves_incorrect_cpu_result(self):
        pending = bytearray()
        guest_smp.append_console_output(
            pending, b'\nSMP_MIGRATED_1=[ 1.0] diagnostic\n0-2\n', filter_guest_logs=True)
        self.assertEqual(pending, b'\nSMP_MIGRATED_1=0-2\n')

    def test_guest_filter_never_hides_panic_split_by_host_record(self):
        data = b'[ 1.0] Kernel pa' + self.warning + b'nic: failure\n'
        for split in range(len(data) + 1):
            with self.subTest(split=split), self.assertRaisesRegex(RuntimeError, 'kernel failure'):
                pending = bytearray()
                for chunk in (data[:split], data[split:]):
                    guest_smp.append_console_output(pending, chunk, filter_guest_logs=True)

    def test_guest_filter_reports_lockup_instead_of_timing_out_on_prompt(self):
        data = (b'[ 110.641] watchdog: BUG: soft lockup - CPU#3 stuck for 21s! '
                b'[migration/3:33]\r\n~ # ')
        interleaved = data.replace(b'soft lockup', b'soft ' + self.warning + b'lockup')
        for record in (data, interleaved):
            for split in range(len(record) + 1):
                with self.subTest(record=record, split=split), self.assertRaisesRegex(
                        RuntimeError, 'kernel failure'):
                    pending = bytearray()
                    for chunk in (record[:split], record[split:]):
                        guest_smp.append_console_output(pending, chunk, filter_guest_logs=True)

    def test_guest_boot_diagnostics_are_preserved(self):
        data = b'[   63.704984] CPU1: Booted secondary processor\r\n~ # '
        self.assertEqual(self.normalize((data,)), data.replace(b'\r', b''))

    def test_native_acceptance_retains_guest_crlf_across_host_records(self):
        expected = b'HYPER_GUEST_CONSOLE_RX\r\n'
        data = b'HYPER_GUEST' + self.warning + b'_CONSOLE' + self.warning + b'_RX\r\n'
        for split in range(len(data) + 1):
            with self.subTest(split=split):
                pending = bytearray()
                for chunk in (data[:split], data[split:]):
                    guest_smp.append_console_output(pending, chunk, preserve_cr=True)
                self.assertEqual(pending, expected)

    def test_preserving_cr_does_not_invent_tty_conversion(self):
        pending = bytearray()
        guest_smp.append_console_output(pending, b'result\n', preserve_cr=True)
        self.assertEqual(pending, b'result\n')

    def test_preserving_cr_still_rejects_fatal_host_records(self):
        with self.assertRaisesRegex(RuntimeError, 'kernel failure'):
            guest_smp.append_console_output(
                bytearray(), b'<0>[ 1.000000] HypeR KERNEL PANIC\r\n', preserve_cr=True)

    def test_host_panic_is_never_filtered_out(self):
        data = b'<0>[ 66.700000] HypeR KERNEL PANIC - NOT SYNCING\r\n'
        for split in range(len(data) + 1):
            with self.subTest(split=split), self.assertRaisesRegex(RuntimeError, 'kernel failure'):
                self.normalize((data[:split], data[split:]))

    def test_guest_panic_split_by_host_log_is_detected(self):
        with self.assertRaisesRegex(RuntimeError, 'kernel failure'):
            self.normalize((b'Kernel pa' + self.warning + b'nic: failure\n',))

    def test_incomplete_log_cannot_fabricate_a_complete_prompt(self):
        pending = self.normalize((b'~' + self.warning[:-2],))
        self.assertNotIn(b'~ # ', pending)
        guest_smp.append_console_output(pending, b'\r\n # ')
        self.assertEqual(pending, b'~ # ')


if __name__ == '__main__':
    unittest.main()
