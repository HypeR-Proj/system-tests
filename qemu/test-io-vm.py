#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Guard against false-positive cross-VM disk acceptance and corrupt packages."""

import gzip
import contextlib
import io
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'io_vm_acceptance', Path(__file__).with_name('verify-io-vm.py'))
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


class DiskAcceptanceTests(unittest.TestCase):
    def test_failure_reason_is_retained_when_prefix_arrives_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            child = directory / 'fixture-process'
            child.write_text('''#!/usr/bin/env python3
import sys, time
sys.stdout.write('IO-VM-SMOKE: FAIL ')
sys.stdout.flush()
time.sleep(0.1)
print('nonidentity DMA setup failed', flush=True)
time.sleep(60)
''')
            child.chmod(0o755)
            log = directory / 'acceptance.log'
            diagnostic = io.StringIO()
            with contextlib.redirect_stderr(diagnostic), self.assertRaisesRegex(
                    RuntimeError, 'fixture reported a failure'):
                acceptance.run(str(child), directory / 'kernel', directory / 'initramfs',
                               log, 5)
            self.assertIn('IO-VM-SMOKE: FAIL nonidentity DMA setup failed',
                          diagnostic.getvalue())
            self.assertIn(b'nonidentity DMA setup failed\n', log.read_bytes())

    def test_runner_checks_disk_after_stopping_its_owned_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            child = directory / 'fixture-process'
            child.write_text('''#!/usr/bin/env python3
import os, sys, time
drive = sys.argv[sys.argv.index('-drive') + 1]
path = next(value[5:] for value in drive.split(',') if value.startswith('file='))
with open(path, 'r+b') as stream:
    stream.seek(4096)
    stream.write(bytes((index + 31 * 17) & 255 for index in range(512)))
    stream.flush()
    os.fsync(stream.fileno())
print('IO-VM-SMOKE: PASS', flush=True)
time.sleep(60)
''')
            child.chmod(0o755)
            with contextlib.redirect_stdout(io.StringIO()):
                acceptance.run(str(child), directory / 'kernel', directory / 'initramfs',
                               directory / 'acceptance.log', 5)
            results = list(directory.glob('acceptance-*/result.json'))
            self.assertEqual(len(results), 1)
            self.assertTrue(json.loads(results[0].read_text())['passed'])

    def test_empty_disk_or_wrong_offset_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'disk.img'
            with path.open('wb') as stream:
                stream.truncate(acceptance.DISK_BYTES)
            with self.assertRaisesRegex(ValueError, 'final business-guest write'):
                acceptance.verify_disk(path)
            with path.open('r+b') as stream:
                stream.seek(acceptance.WRITE_OFFSET + 512)
                stream.write(acceptance.FINAL_PATTERN)
            with self.assertRaises(ValueError):
                acceptance.verify_disk(path)

    def test_correct_final_round_requires_other_disk_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'disk.img'
            with path.open('wb') as stream:
                stream.truncate(acceptance.DISK_BYTES)
                stream.seek(acceptance.WRITE_OFFSET)
                stream.write(acceptance.FINAL_PATTERN)
            acceptance.verify_disk(path)
            with path.open('r+b') as stream:
                stream.seek(acceptance.DISK_BYTES - 1)
                stream.write(b'\xff')
            with self.assertRaisesRegex(ValueError, 'after the test extent'):
                acceptance.verify_disk(path)

    def test_split_marker_with_interleaved_host_log_and_failure(self):
        message = b'IO-VM-SMOKE: PA<6>[  1.000] HypeR: host log\r\nSS\r\n'
        for split in range(len(message)):
            pending = bytearray()
            acceptance.append_output(pending, message[:split])
            self.assertTrue(acceptance.append_output(pending, message[split:]))
        with self.assertRaises(RuntimeError):
            acceptance.append_output(bytearray(), b'IO-VM-SMOKE: PASS\nKernel panic\n')

    def test_boot_generation_checksums_prevent_mixed_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            image = bytearray(64)
            image[56:60] = b'ARM\x64'
            ramdisk = gzip.compress(b'070701fixture', mtime=0)
            kernel_name = 'Image-' + hashlib.sha256(image).hexdigest()
            ramdisk_name = 'initramfs-' + hashlib.sha256(ramdisk).hexdigest() + '.cpio.gz'
            (package / kernel_name).write_bytes(image)
            (package / ramdisk_name).write_bytes(ramdisk)
            manifest = {'format': 1, 'architecture': 'aarch64', 'kernel': kernel_name,
                        'initramfs': ramdisk_name}
            (package / 'boot-artifacts.json').write_text(json.dumps(manifest))
            self.assertEqual(acceptance.package_payloads(package),
                             ((package / kernel_name).resolve(), (package / ramdisk_name).resolve()))
            with self.assertRaisesRegex(ValueError, 'hyper.clients.v5'):
                acceptance.package_payloads(package, require_client_resources=True)
            manifest['client_table_version'] = 5
            (package / 'boot-artifacts.json').write_text(json.dumps(manifest))
            self.assertEqual(acceptance.package_payloads(package, require_client_resources=True),
                             ((package / kernel_name).resolve(), (package / ramdisk_name).resolve()))
            (package / kernel_name).write_bytes(image + b'corruption')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                acceptance.package_payloads(package)
            manifest['kernel'] = '../Image'
            (package / 'boot-artifacts.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'filename'):
                acceptance.package_payloads(package)


if __name__ == '__main__':
    unittest.main()
