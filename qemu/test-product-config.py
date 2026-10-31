#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Check the trimmed-product scenario's evidence and fixture composition."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('product_scenario', Path(__file__).with_name('verify-product-config.py'))
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)


class ProductScenarioTests(unittest.TestCase):
    def test_preparation_adds_only_the_reporter_and_keeps_content_identity(self):
        entries = scenario.load_registry(scenario.ROOT / 'mk/components.mk')['entries']
        original = scenario.system_config.ProductConfig(scenario.trimmed_product(entries))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'product.config'
            scenario.prepare(output)
            configured = scenario.system_config.load(output).value
            self.assertEqual(set(configured['system']['include']) - set(original.value['system']['include']),
                             {'test-report'})
            configured['system']['include'].remove('test-report')
            restored = scenario.system_config.ProductConfig(configured)
            self.assertEqual(restored.select(entries), original.select(entries))
            first = output.stat().st_mtime_ns
            scenario.prepare(output)
            self.assertEqual(output.stat().st_mtime_ns, first)

    def test_executable_inventory_rejects_missing_extra_and_duplicate_entries(self):
        scenario.program_inventory(b'\ncat\nls\n', {'cat', 'ls'}, 'bin')
        for output in (b'cat\n', b'cat\nls\nvmm\n', b'cat\nls\nls\n'):
            with self.subTest(output=output), self.assertRaises(AssertionError):
                scenario.program_inventory(output, {'cat', 'ls'}, 'bin')

    def test_service_evidence_requires_retirement_and_running_providers(self):
        healthy = b''.join(f'/{name} running 1 0 0x123\n'.encode() for name in
                           ('console-raw', 'io-runtime', 'fs-backend', 'journal', 'session-manager'))
        healthy += b'Startup progress: ownership-transferred, bootstrap-retired\n'
        scenario.service_inventory(healthy)
        for output in (healthy.replace(b'/io-runtime running', b'/io-runtime stopped'),
                       healthy.replace(b'bootstrap-retired', b'retirement-blocked'),
                       healthy + b'/vm-manager stopped 0 0 -\n'):
            with self.subTest(output=output), self.assertRaises(AssertionError):
                scenario.service_inventory(output)


if __name__ == '__main__':
    unittest.main()
