#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Local packaging and collector checks; no network connections or measurements."""

import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load('prepare')
exercise = load('exercise')


class NetworkTools(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def base(self, **replacements):
        files = {'etc/alpine-release': b'3.23.5\n',
                 'lib/ld-musl-aarch64.so.1': b'loader',
                 'usr/lib/libcrypto.so.3': b'crypto',
                 'usr/bin/storage-qual': b'unchanged workload'}
        files.update(replacements)
        path = self.root / 'base.tar'
        with tarfile.open(path, 'w') as archive:
            for name, data in files.items():
                if data is not None:
                    prepare.add_file(archive, name, data)
        return path

    def package(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as target:
            for name in ('.PKGINFO', 'usr/bin/iperf3', 'usr/lib/libiperf.so.0.0.0'):
                prepare.add_file(target, name, name.encode())
            link = tarfile.TarInfo('usr/lib/libiperf.so.0')
            link.type, link.linkname = tarfile.SYMTYPE, 'libiperf.so.0.0.0'
            target.addfile(link)
        path = self.root / 'iperf3.apk'
        path.write_bytes(gzip.compress(archive.getvalue()))
        return path

    def test_package_retains_base_libraries_workload_and_notices(self):
        base, package = self.base(), self.package()
        before = prepare.digest(base)
        license_file = self.root / 'LICENSE'
        license_file.write_bytes(b'upstream notices')
        output = self.root / 'result.tar'
        prepare.rootfs(base, package, license_file, output)
        self.assertEqual(before, prepare.digest(base))
        with tarfile.open(output) as archive:
            self.assertEqual(archive.extractfile('usr/bin/storage-qual').read(), b'unchanged workload')
            self.assertEqual(archive.extractfile('usr/lib/libcrypto.so.3').read(), b'crypto')
            self.assertEqual(archive.extractfile('opt/hyper-network/LICENSE').read(), b'upstream notices')
            self.assertEqual(archive.getmember('usr/bin/iperf3').mode, 0o755)
            self.assertEqual(archive.getmember('opt/hyper-network/usr/lib/libiperf.so.0').linkname,
                             'libiperf.so.0.0.0')
            self.assertNotIn('usr/lib/libiperf.so.0', archive.getnames())

    def test_incompatible_or_already_modified_base_is_rejected(self):
        for change in ({'etc/alpine-release': b'3.24.0'},
                       {'usr/lib/libcrypto.so.3': None},
                       {'opt/hyper-network/existing': b'already packaged'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                prepare.rootfs(self.base(**change), self.package(), self.root / 'unused',
                               self.root / 'result.tar')

    def collect(self, run):
        output = self.root / 'results'
        with patch.dict(os.environ, {'CI': '', 'GITHUB_ACTIONS': ''}), \
                patch('sys.argv', ['exercise.py', '192.0.2.1', '--label', 'raspbian',
                                   '--output', str(output)]), \
                patch.object(exercise.platform, 'platform', return_value='test client'), \
                patch.object(exercise.subprocess, 'run', side_effect=run):
            exercise.main()

    def test_directions_and_receiver_metric(self):
        commands = []

        def run(command, **kwargs):
            if '--version' not in command:
                commands.append(command)
                json.dump({'end': {'sum_received': {'bits_per_second': 123000000},
                                   'sum_sent': {'bits_per_second': 999000000, 'retransmits': 3}}},
                          kwargs['stdout'])
            return subprocess.CompletedProcess(command, 0, stdout='iperf3 test')

        self.collect(run)
        self.assertEqual([('-R' in c, c[c.index('-P') + 1]) for c in commands],
                         [(False, '1'), (True, '1'), (False, '4'), (True, '4')])
        summary = json.loads((self.root / 'results/summary.json').read_text())
        self.assertEqual([r['receiver_mbit_s'] for r in summary['measurements']], [123.0] * 4)
        self.assertTrue(all(r['name'].startswith('raspbian-') for r in summary['measurements']))

    def test_timeout_retains_stdout_stderr_and_failure_record(self):
        def run(command, **kwargs):
            if '--version' in command:
                return subprocess.CompletedProcess(command, 0, stdout='iperf3 test')
            kwargs['stdout'].write('{partial')
            kwargs['stderr'].write('timeout diagnostic')
            raise subprocess.TimeoutExpired(command, 45)

        with self.assertRaises(SystemExit):
            self.collect(run)
        output = self.root / 'results'
        self.assertEqual((output / 'raspbian-rx-p1.json').read_text(), '{partial')
        self.assertEqual((output / 'raspbian-rx-p1.stderr').read_text(), 'timeout diagnostic')
        rows = json.loads((output / 'summary.json').read_text())['measurements']
        self.assertEqual(len(rows), 1)
        self.assertIn('error', rows[0])
        self.assertNotIn('receiver_mbit_s', rows[0])


if __name__ == '__main__':
    unittest.main()
