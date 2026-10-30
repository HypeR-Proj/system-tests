#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Keep bootstrap retirement acceptance strict about ownership and backing."""

import importlib.util
from itertools import repeat
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    'boot_system', Path(__file__).with_name('verify-boot-system.py'))
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)


class BootDisplayTests(unittest.TestCase):
    ROW = b'  \x1b[32m[RUNNING]\x1b[0m /fs-backend  Filesystem mount service  \x1b[2mstart 41.91 ms\x1b[0m\r\n'
    REST = (
        b'  [RUNNING] /vm-manager Virtual machine fleet start 90.60 ms\r\n'
        b'  [START  ] /bootstrap-retire Retire bootstrap services\r\n'
        b'INFO: /bootstrap-retire generation=1 exited successfully (status 0); releasing resources; process=0x123\r\n'
        b'  [DONE   ] /bootstrap-retire Retire bootstrap services total 355.03 ms\r\n'
        b'  >> bootstrap retired +1.600s\r\n'
        b'HypeR: startup complete\r\n'
        b'hyper-sh$ '
    )

    def test_current_display_accepts_color_crlf_and_variable_timing(self):
        boot.check_boot_display(self.ROW + self.REST)
        boot.check_boot_display((self.ROW + self.REST).replace(b'41.91', b'1234.56'))

    def test_stack_audit_can_interrupt_any_byte_in_a_status_row(self):
        record = b'<6>[    0.425119] HypeR STACK-AUDIT kind=user owner=component-manager\r\n'
        for offset in range(len(self.ROW) + 1):
            with self.subTest(offset=offset):
                boot.check_boot_display(self.ROW[:offset] + record + self.ROW[offset:] + self.REST)

    def test_filter_must_not_hide_kernel_failure(self):
        with self.assertRaises(RuntimeError):
            boot.check_boot_display(b'<0>[ 1.000000] HypeR KERNEL PANIC\n' + self.ROW + self.REST)

    def test_missing_duplicate_and_incorrect_duration_are_rejected(self):
        for row in (b'', self.ROW * 2, self.ROW.replace(b'start 41.91 ms', b'41.91 ms'),
                    self.ROW.replace(b'start', b'total'), self.ROW.replace(b'41.91', b'41.9')):
            with self.subTest(row=row), self.assertRaises(AssertionError):
                boot.check_boot_display(row + self.REST)
        with self.assertRaises(AssertionError):
            boot.check_boot_display(self.ROW + self.REST.replace(b'total 355.03 ms', b'start 355.03 ms'))

    def test_timing_on_next_line_cannot_complete_a_broken_row(self):
        with self.assertRaises(AssertionError):
            boot.check_boot_display(self.ROW.replace(b'start 41.91 ms', b'\r\nstart 41.91 ms') + self.REST)


class RecoveryConsoleTests(unittest.TestCase):
    def test_kernel_record_cannot_split_the_recovery_diagnostic(self):
        # Replay the main-branch failure: recovery actually completed, but a
        # stack-audit record interrupted init's emergency Console write.
        raw = (
            b'[    0.414414][HypeR bootstrap stage 1][init] system init'
            b'<6>[    0.425119] HypeR STACK-AUDIT kind=user owner=io-runtime\n'
            b' unavailable; retaining core supervision and recovery shell\n'
            b'[    0.486403][HypeR bootstrap stage 1][fallback-sh] recovery console ready\n'
            b'fallback-sh$ '
        )
        script = (
            'import select,sys,time\n'
            f'sys.stdout.buffer.write({raw!r}); sys.stdout.flush()\n'
            'for command, result in [(b"pwd\\n", b"/\\n"), '
            '(b"help\\n", b"Recovery shell:\\n")]:\n'
            '    if not select.select([sys.stdin], [], [], 3)[0]: sys.exit(2)\n'
            '    assert sys.stdin.buffer.readline() == command\n'
            '    sys.stdout.buffer.write(b"\\n" + result + b"fallback-sh$ ")\n'
            '    sys.stdout.flush()\n'
            'time.sleep(30)\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'recovery.log'
            argv = ['verify-boot-system.py', '--qemu', 'fixture', '--image', 'image',
                    '--initramfs', 'archive', '--log', str(log), '--recovery']
            with patch.object(sys, 'argv', argv), patch.object(
                    boot, 'native_command', return_value=[sys.executable, '-u', '-c', script]):
                boot.main()
            self.assertIn(raw, log.read_bytes())


class MemoryAccountingTests(unittest.TestCase):
    def report(self, *, complete=True, retained=20480):
        derived = ('278528 B', '49152 B', '131072 B') if complete else ('—', '—', '—')
        retained = f'{retained} B' if complete else '—'
        return (
            f'Mem: 409600 B {derived[0]} 81920 B {derived[1]} {derived[2]}\n'
            'Reserved: 40960 B  Managed: 368640 B  Allocated: 286720 B\n'
            'Owners: kernel=94208 B user=40960 B guest=81920 B '
            'buffered/cached=69632 B other=0 B\n'
            f'          non-reclaimable={retained} (included in used and buffered/cached)\n'
        ).encode()

    def test_baseline_retries_a_concurrent_incomplete_census(self):
        session = Mock()
        run = Mock(side_effect=[self.report(complete=False), self.report()])
        with patch.object(boot.time, 'monotonic', return_value=0):
            report = boot.memory_baseline(session, run)
        self.assertEqual(report['used'], 278528)
        session.pump.assert_called_once_with(0.5)

    def test_baseline_cannot_wait_forever_for_a_complete_census(self):
        run = Mock(return_value=self.report(complete=False))
        with patch.object(boot.time, 'monotonic', side_effect=[0, 31]), \
                self.assertRaisesRegex(AssertionError, 'no complete cache census'):
            boot.memory_baseline(Mock(), run)

    def test_complete_census_must_reconcile_retained_cache_with_used(self):
        # Total/free arithmetic and physical owners are internally consistent,
        # but omitting pinned cache from the used breakdown must still fail.
        session = Mock()
        with self.assertRaisesRegex(AssertionError, 'used and cache breakdown'):
            boot.memory_baseline(session, Mock(return_value=self.report(retained=0)))
        session.pump.assert_not_called()


class RetirementEvidenceTests(unittest.TestCase):
    def check(self, *, extra_process=b'', boot_entries=b'', reserved=4096):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'console.log'
            log.write_bytes(
                b'HypeR: memory (kernel initialized): 1024 MiB RAM, 10 reserved pages\n'
                b'HypeR bootstrap-retire: bootstrap retired; boot archive detached\n')
            processes = b''.join(
                b'process 0x01 - ' + name + b' running threads=1 pending=0\n'
                for name in (b'console-raw', b'io-runtime', b'fs-backend', b'component-manager'))
            outputs = {
                'ls /boot/init': b'ls: /boot/init: not found\n',
                'ls -1 /boot': boot_entries,
                'ps --no-headers': processes + extra_process,
            }
            reservations = iter(reserved) if isinstance(reserved, tuple) else repeat(reserved)

            def run(command, expected=None, **kwargs):
                self.assertEqual(kwargs.get('fails', False), command == 'ls /boot/init')
                if command == 'free --bytes':
                    return f'Reserved: {next(reservations)} B\n'.encode()
                return outputs[command]

            boot.retired_bootstrap(Mock(), run, log)

    def test_independent_formal_owners_and_reclaimed_disk_archive_pass(self):
        self.check()

    def test_retirement_banner_cannot_hide_a_live_predecessor(self):
        for name in (b'init', b'fallback-sh', b'io-runtime', b'fs-backend', b'console-raw'):
            with self.subTest(name=name), self.assertRaises(AssertionError):
                self.check(extra_process=b'process 0x02 - ' + name + b' running threads=1\n')

    def test_retired_process_record_does_not_count_as_a_live_predecessor(self):
        self.check(extra_process=b'process 0x02 - init retired threads=0\n')

    def test_detached_archive_cannot_remain_accessible_by_path(self):
        with self.assertRaisesRegex(AssertionError, 'remains reachable'):
            self.check(boot_entries=b'svc/\n')

    def test_archive_reclamation_is_required(self):
        with patch.object(boot.time, 'monotonic', side_effect=(0, 0, 31)), \
                self.assertRaisesRegex(AssertionError, 'reservation did not shrink'):
            self.check(reserved=40960)

    def test_last_retired_image_may_release_after_namespace_detachment(self):
        with patch.object(boot.time, 'monotonic', side_effect=(0, 0, 1)):
            self.check(reserved=(40960, 4096))


class RootHandoffEvidenceTests(unittest.TestCase):
    def test_formal_programs_live_at_root_without_system_alias(self):
        root = b'bin/\netc/\nlib@\nlib64/\nsvc/\nboot/\ndata/\ntmp/\nvm/\n'
        boot.check_root_layout(root)
        with self.assertRaises(AssertionError):
            boot.check_root_layout(root + b'system/\n')
        with self.assertRaises(AssertionError):
            boot.check_root_layout(b'boot/\nsystem/\ndata/\ntmp/\n')

    def test_root_is_switched_before_candidate_adopts_owners(self):
        markers = (
            b'EROFS mounted at /system',
            b'[HypeR bootstrap stage 1][init] provisional system root active; preparing successor',
            b'HypeR system-startup: core supervision adopted',
            b'FAT mounted at /data', b'HypeR system-startup: VM fleet configured',
            b'HypeR bootstrap-retire: bootstrap retired; boot archive detached',
        )
        boot.check_handoff_order(b'\n'.join(markers))
        with self.assertRaises(AssertionError):
            boot.check_handoff_order(b'\n'.join((markers[0], markers[2], markers[1], *markers[3:])))

    def test_recovery_requires_namespace_rollback_before_prompt(self):
        markers = (
            b'[HypeR bootstrap stage 1][init] provisional system root active; preparing successor',
            b'[HypeR bootstrap stage 1][init] system handoff rejected; bootstrap root restored',
            b'[HypeR bootstrap stage 1][fallback-sh] recovery console ready',
        )
        boot.check_rollback_order(b'\n'.join(markers))
        with self.assertRaises(AssertionError):
            boot.check_rollback_order(b'\n'.join((markers[0], markers[2], markers[1])))
        with self.assertRaises(AssertionError):
            boot.check_rollback_order(b'\n'.join((markers[0], markers[2])))
        for commit in (b'core supervision transferred', b'core supervision adopted'):
            with self.assertRaises(AssertionError):
                boot.check_rollback_order(b'\n'.join((*markers, commit)))


class ProviderRejectionEvidenceTests(unittest.TestCase):
    def evidence(self, case):
        common = (
            b'HypeR system-startup: core supervision adopted',
            b'HypeR system-startup: provider adoption failed: bootstrap retirement disabled; '
            b'replacement preparation failed; bootstrap provider retained',
            b'[HypeR bootstrap stage 1][fs-backend] adoption preparation failed; bootstrap mounts retained: closed',
            b'HypeR session: console ready',
        )
        if case == 'missing-fs-erofs':
            common += (b'HypeR io-runtime: running backend adopted',
                       b'EROFS recognized for /, but filesystem worker is unavailable')
        return b'\n'.join(common)

    def test_expected_preparation_failures_preserve_formal_session(self):
        for case in boot.PROVIDER_REJECTIONS:
            boot.check_provider_rejection(case, self.evidence(case))

    def test_wrong_failure_boundary_or_retirement_cannot_pass(self):
        for case in boot.PROVIDER_REJECTIONS:
            evidence = self.evidence(case)
            for extra in (b'HypeR bootstrap-retire: bootstrap retired; boot archive detached',
                          b'[HypeR bootstrap stage 1][fallback-sh] recovery console ready',
                          b'HypeR fs-backend: filesystem ownership adopted'):
                with self.subTest(case=case, extra=extra), self.assertRaises(AssertionError):
                    boot.check_provider_rejection(case, evidence + b'\n' + extra)
        with self.assertRaises(AssertionError):
            boot.check_provider_rejection('missing-fs-erofs', self.evidence('missing-io-runtime'))
        with self.assertRaises(AssertionError):
            boot.check_provider_rejection('missing-io-runtime', self.evidence('missing-fs-erofs'))

    def test_bootstrap_diagnostic_can_be_split_by_successor_mirror(self):
        bootstrap = (b'[    5.286838][HypeR bootstrap stage 1][fs-backend] '
                     b'adoption preparation failed; bootstrap mounts retained: InvalidResponse\n')
        for case in boot.PROVIDER_REJECTIONS:
            lines = self.evidence(case).splitlines(keepends=True)
            # CI observed the formal failure report inside the bootstrap prefix.
            # Both mirrors remain live when replacement preparation is rejected.
            for offset in range(len(bootstrap)):
                evidence = (lines[0] + bootstrap[:offset] + lines[1]
                            + bootstrap[offset:] + b''.join(lines[3:]))
                with self.subTest(case=case, offset=offset):
                    boot.check_provider_rejection(case, evidence)

    def test_recovery_requires_successful_administration_and_persistent_io(self):
        responses = {
            'ls /boot/init': b'init\n',
            'cat /etc/hyper/components.json': b'configuration_directory\n',
            'mount': b'PARTUUID=test on / type erofs (ro)\n',
            'cat /data/provider-rejection.txt': b'\nPROVIDER-REJECTION-IO\n',
        }
        for name in ('component-manager', 'io-runtime', 'fs-backend', 'fs-erofs'):
            responses[f'ps --name {name}'] = name.encode() + b' running\n'

        def exercise(fail_at=None, corrupt_read=False):
            session = Mock()
            seen = []

            def run(command, expected=None):
                seen.append(command)
                rc = 1 if len(seen) == fail_at else 0
                output = responses.get(command, b'')
                if corrupt_read and command == 'cat /data/provider-rejection.txt':
                    output = b'wrong content\n'
                session.await_text.return_value = (
                    output + f'\nHYPER-COMMAND-STATUS={rc}\nhyper-sh$ '.encode())
                return boot.command(session, command, expected)

            boot.retained_provider_io(run)
            return seen

        commands = exercise()
        # Each transition and read must fail the scenario even if the earlier
        # diagnostic claims the old filesystem provider survived.
        for position in range(1, len(commands) + 1):
            with self.subTest(command=commands[position - 1], position=position):
                with self.assertRaises(AssertionError):
                    exercise(fail_at=position)
        with self.assertRaises(AssertionError):
            exercise(corrupt_read=True)

    def test_fault_fixture_only_changes_its_new_system_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'original'
            (source / 'svc').mkdir(parents=True)
            for name in boot.PROVIDER_REJECTIONS.values():
                (source / 'svc' / name).write_bytes(b'original executable')
            for case, missing in boot.PROVIDER_REJECTIONS.items():
                output = root / case
                with patch.object(boot.subprocess, 'run') as pack:
                    boot.prepare_provider_rejection([
                        '--case', case, '--system-root', str(source),
                        '--board', str(root / 'board.json'), '--output', str(output),
                        '--mkfs-erofs', '/test/mkfs.erofs', '--artifact', 'alpine=/test/alpine.itb',
                    ])
                self.assertTrue((source / 'svc' / missing).is_file())
                self.assertFalse((output / 'system-root/svc' / missing).exists())
                invocation = pack.call_args.args[0]
                self.assertIn(str(output / 'disk.img'), invocation)
                self.assertIn('alpine=/test/alpine.itb', invocation)
                self.assertNotIn('--replace', invocation)


if __name__ == '__main__':
    unittest.main()
