#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Check that the hotplug harness cannot accept missing or reordered evidence."""

import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'console_hotplug', Path(__file__).with_name('verify-console-hotplug.py'))
hotplug = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hotplug)


class HotplugAcceptance(unittest.TestCase):
    def test_every_evidence_marker_is_required(self):
        output = b'\n'.join(hotplug.MARKERS) + b'\n'
        hotplug.verify_markers(output)
        for marker in hotplug.MARKERS:
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                hotplug.verify_markers(output.replace(marker + b'\n', b''))

    def test_early_presence_and_reversed_rounds_do_not_pass(self):
        for first, second in ((0, 1), (4, 5)):
            markers = list(hotplug.MARKERS)
            markers[first], markers[second] = markers[second], markers[first]
            with self.subTest(first=first), self.assertRaises(ValueError):
                hotplug.verify_markers(b'\n'.join(markers) + b'\n')

    def test_probe_launch_is_between_device_mounts_and_original_backend(self):
        init = (b'#!/bin/sh\nset -eu\nmount -t sysfs sysfs /sys\n' + hotplug.MOUNT
                + b'exec /usr/bin/hyper-io-supervisor\n')
        archive = hotplug.overlay({'init': init})
        modified = hotplug.init_script(archive)
        self.assertEqual(modified, init.replace(hotplug.MOUNT, hotplug.MOUNT + hotplug.START_PROBE))
        with self.assertRaises(ValueError):
            hotplug.init_script(archive[:-20])
        with self.assertRaises(ValueError):
            hotplug.init_script(hotplug.overlay({'init': b'#!/bin/sh\nexec /backend\n'}))

    def test_probe_rejects_wrong_architecture_and_dynamic_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'probe'
            data = bytearray(120)
            data[:7] = b'\x7fELF\x02\x01\x01'
            struct.pack_into('<H', data, 18, 183)
            struct.pack_into('<Q', data, 32, 64)
            struct.pack_into('<HH', data, 54, 56, 1)
            struct.pack_into('<I', data, 64, 1)
            path.write_bytes(data)
            self.assertEqual(hotplug.static_probe(path), data)
            struct.pack_into('<I', data, 64, 3)
            path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'statically linked'):
                hotplug.static_probe(path)
            struct.pack_into('<H', data, 18, 62)
            path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'AArch64'):
                hotplug.static_probe(path)


if __name__ == '__main__':
    unittest.main()
