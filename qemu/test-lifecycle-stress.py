#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""The stress oracle rejects partial, stale and duplicate evidence."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('stress', Path(__file__).with_name('verify-lifecycle-stress.py'))
stress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stress)

GOOD = (b'STRESS_BEGIN round=0 seed=175 normal_exit=false\n'
        b'STRESS_PASS round=0 transfers=3\n'
        b'STRESS_BEGIN round=1 seed=176 normal_exit=true\n'
        b'STRESS_PASS round=1 transfers=2\n'
        b'LIFECYCLE_STRESS_OK seed=175 rounds=2\n')


class StressOracle(unittest.TestCase):
    def test_complete(self):
        stress.validate(GOOD, 175, 2)

    def test_invalid_evidence(self):
        for changed in (
            GOOD.replace(b'STRESS_PASS round=1 transfers=2\n', b''),
            GOOD + b'STRESS_PASS round=1 transfers=2\n',
            GOOD.replace(b'transfers=3', b'transfers=0'),
            GOOD.replace(b'seed=175', b'seed=174'),
            GOOD.replace(b'normal_exit=true', b'normal_exit=false'),
            GOOD.replace(b'round=1 transfers=2', b'round=0 transfers=2'),
            GOOD.replace(b'LIFECYCLE_STRESS_OK seed=175 rounds=2\n', b''),
        ):
            with self.subTest(output=changed), self.assertRaises(AssertionError):
                stress.validate(changed, 175, 2)

    def test_wrapping_seed(self):
        output = (b'STRESS_BEGIN round=0 seed=18446744073709551615 normal_exit=false\n'
                  b'STRESS_PASS round=0 transfers=1\n'
                  b'STRESS_BEGIN round=1 seed=0 normal_exit=false\n'
                  b'STRESS_PASS round=1 transfers=1\n'
                  b'LIFECYCLE_STRESS_OK seed=18446744073709551615 rounds=2\n')
        stress.validate(output, 2**64 - 1, 2)


if __name__ == '__main__':
    unittest.main()
