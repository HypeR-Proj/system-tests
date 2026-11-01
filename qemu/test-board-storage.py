#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Failure diagnosis must retain the acceptance result and use matching inputs."""

import contextlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('board_storage', Path(__file__).with_name('verify-board-storage.py'))
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class StackDiagnosisTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.args = SimpleNamespace(
            log=root / 'accept', disk=root / 'disk.img', image=root / 'hyper.img',
            stack_elf=root / 'hyper', stack_config=root / 'stack16.config',
            qemu='qemu-system-aarch64', observed_name='io', verify_only=True,
            minimum_stack_remaining=4096, maximum_stack_used=24576)
        self.args.log.with_suffix('.verify.log').write_bytes(
            b'HypeR STACK-AUDIT kind=user used=12776 remaining=3600 size=16384 canary=true\n')

    def test_diagnostic_failure_preserves_original_stack_failure(self):
        for failure in (None, OSError('unavailable')):
            with self.subTest(failure=failure), contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(storage.subprocess, 'run', side_effect=failure,
                                 return_value=SimpleNamespace(returncode=1)) as run:
                with self.assertRaisesRegex(ValueError, 'user stack reserve 3600 < 4096'):
                    storage.audit_stacks(self.args)
                command = run.call_args.args[0]
                self.assertIn(str(self.args.stack_elf), command)
                self.assertIn(str(self.args.stack_config), command)
                self.assertEqual(command[command.index('--fixture') + 1], str(self.args.disk.parent))
                self.assertEqual(command[command.index('--timeout') + 1], '90')
                self.assertFalse(run.call_args.kwargs['check'])

    def test_without_matching_inputs_the_failure_is_still_reported(self):
        self.args.stack_elf = self.args.stack_config = None
        with patch.object(storage.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'stack reserve'):
                storage.audit_stacks(self.args)
            run.assert_not_called()

    def test_success_does_not_boot_a_diagnostic_vm(self):
        self.args.log.with_suffix('.verify.log').write_bytes(b''.join(
            f'HypeR STACK-AUDIT kind={kind} used=100 remaining=16276 size=16384 canary=true\n'.encode()
            for kind in ('kernel', 'user', 'irq')))
        with patch.object(storage.subprocess, 'run') as run:
            storage.audit_stacks(self.args)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
