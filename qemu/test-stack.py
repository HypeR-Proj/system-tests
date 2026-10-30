#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Host regression tests for stack audit acceptance, without starting QEMU."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'stack_check', Path(__file__).with_name('verify-stack.py'))
stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack)


def record(kind, used=4096, canary='true'):
    return (f'<6>[ 1.000000] HypeR STACK-AUDIT kind={kind} owner=test '
            f'samples=1 used={used} remaining={32760 - used} '
            f'size=32768 canary={canary}\r\n').encode()


class AuditTests(unittest.TestCase):
    def test_maximum_is_not_last_observation(self):
        data = (record('user', 9000) + record('user', 8000) + record('irq')
                + record('vcpu') + record('kernel'))
        self.assertEqual(stack.audit_summary(data, 16000)['user'], 9000)

    def test_missing_category_rejected(self):
        with self.assertRaisesRegex(ValueError, 'missing'):
            stack.audit_summary(record('user') + record('irq'), 0)

    def test_canary_failure_never_hidden_by_later_record(self):
        with self.assertRaisesRegex(ValueError, 'canary'):
            stack.audit_summary(record('user', canary='false') + record('user'), 0)

    def test_reserve_threshold(self):
        with self.assertRaisesRegex(ValueError, 'reserve'):
            stack.audit_summary(record('user', 32000), 1024)

    def test_usage_limit_is_independent_of_allocated_stack_size(self):
        with self.assertRaisesRegex(ValueError, 'usage'):
            stack.audit_summary(record('user', 9000), 1024, maximum_used=8192)

    def test_usage_limit_accepts_the_boundary(self):
        data = b''.join(record(kind, 8192) for kind in ('user', 'kernel', 'irq', 'vcpu'))
        self.assertEqual(stack.audit_summary(data, 1024, maximum_used=8192)['user'], 8192)

    def test_malformed_record_is_not_ignored(self):
        with self.assertRaisesRegex(ValueError, 'malformed'):
            stack.audit_summary(b'HypeR STACK-AUDIT kind=user used=oops\n', 0)

    def test_inconsistent_accounting_rejected(self):
        with self.assertRaisesRegex(ValueError, 'accounting'):
            stack.audit_summary(record('user').replace(b'size=32768', b'size=65536'), 0)


if __name__ == '__main__':
    unittest.main()
