#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Ensure fleet acceptance cannot confuse guest failure with rejected admission."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'fleet_config', Path(__file__).with_name('verify-fleet-config.py'))
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)


class FleetLogTests(unittest.TestCase):
    def test_configuration_rejection_requires_surviving_native_services(self):
        output = fleet.REJECTED + b' missing image\n' + fleet.DEGRADED
        fleet.validate_log('missing-image', output)
        for marker in fleet.INIT_FAILURES:
            with self.subTest(marker=marker), self.assertRaises(AssertionError):
                fleet.validate_log('missing-image', output + b'\n' + marker)

    def test_guest_or_unrelated_service_failure_is_not_configuration_rejection(self):
        for output in (
            b'alpine failed\n' + fleet.BOOT_FAILED,
            fleet.REJECTED + b' missing image\n',
            b"HypeR system-startup: critical service 'session' terminated\n" + fleet.BOOT_FAILED,
        ):
            with self.assertRaises(AssertionError):
                fleet.validate_log('malformed', output)

    def test_rejected_batch_cannot_publish_success_or_start_a_guest(self):
        rejected = fleet.REJECTED + b'\n' + fleet.DEGRADED
        for extra in (fleet.CONFIGURED, fleet.GUEST_RUNNING):
            with self.assertRaises(AssertionError):
                fleet.validate_log('missing-image', rejected + b'\n' + extra)

    def test_kernel_failure_cannot_hide_behind_expected_storage_failure(self):
        rejected = fleet.REJECTED + b'\n' + fleet.DEGRADED
        for marker in fleet.KERNEL_FAILURES:
            with self.assertRaises(AssertionError):
                fleet.validate_log('malformed', rejected + b'\n' + marker)

    def test_idle_fleet_must_be_acknowledged_without_starting_a_guest(self):
        for case in ('empty', 'no-autostart'):
            fleet.validate_log(case, fleet.CONFIGURED)
            for output in (b'', fleet.CONFIGURED + b'\n' + fleet.GUEST_RUNNING,
                           fleet.CONFIGURED + b'\n' + fleet.BOOT_FAILED):
                with self.assertRaises(AssertionError):
                    fleet.validate_log(case, output)


if __name__ == '__main__':
    unittest.main()
