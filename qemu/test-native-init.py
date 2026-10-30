#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Native acceptance evidence remains meaningful without a periodic tick."""

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'native_init', Path(__file__).with_name('verify-native-init.py'))
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)


class RuntimeAccountingTests(unittest.TestCase):
    def test_complete_elapsed_runtime_summary_accepts_rounding(self):
        scenario.verify_top_summary(
            b'top - 4 CPUs\nCPU: user-thread 12.34% kthread 1.23% vcpu 40.00% idle 46.41%\n', 4)
        scenario.verify_top_summary(
            b'top - 1 CPUs\nCPU: user-thread 0.00% kthread 0.00% vcpu 0.00% idle 100.00%\n', 1)

    def test_wrong_cpu_count_empty_accounting_and_overflow_are_rejected(self):
        for sample in (
            b'top - 1 CPUs\nCPU: user-thread 25.00% kthread 25.00% vcpu 25.00% idle 25.00%\n',
            b'top - 4 CPUs\nCPU: user-thread 0.00% kthread 0.00% vcpu 0.00% idle 0.00%\n',
            b'top - 4 CPUs\nCPU: user-thread 100.01% kthread 0.00% vcpu 0.00% idle 0.00%\n',
            b'top - 4 CPUs\nCPU: user-thread 1.00% kthread 1.00% vcpu 1.00% idle 1.00%\n',
        ):
            with self.subTest(sample=sample), self.assertRaises(AssertionError):
                scenario.verify_top_summary(sample, 4)

    def test_incomplete_or_obsolete_tick_summary_is_rejected(self):
        for sample in (
            b'top - 4 CPUs ticks=100 Hz\n',
            b'top - 4 CPUs\nCPU: user-thread 10.00% idle 90.00%\n',
        ):
            with self.subTest(sample=sample), self.assertRaises(AssertionError):
                scenario.verify_top_summary(sample, 4)


if __name__ == '__main__':
    unittest.main()
