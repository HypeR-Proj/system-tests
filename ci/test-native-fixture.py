#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Reject stale, mismatched and damaged CI image handoffs."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    'native_fixture', Path(__file__).with_name('native-fixture.py'))
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'artifact'
        for arch, target in fixture.TARGETS.items():
            image = self.root / 'kernel/target' / target / 'kernel/hyper.img'
            image.parent.mkdir(parents=True)
            image.write_bytes(arch.encode() + b' kernel')
            archive = self.root / 'target/app' / arch / 'initramfs.cpio'
            archive.parent.mkdir(parents=True)
            archive.write_bytes(arch.encode() + b' archive')

    def test_round_trip_both_architectures(self):
        for arch in fixture.TARGETS:
            fixture.publish(self.root, self.output, arch, 'revision-a')
            fixture.verify(self.output, arch, 'revision-a')

    def test_configuration_identity_ignores_location_and_comments_but_tracks_options(self):
        first, second = self.root / 'first.config', self.root / 'second.config'
        first.write_text('# generated\nCONFIG_DEBUG=y\n# CONFIG_TRACE is not set\n')
        second.write_text('# another checkout\nCONFIG_TRACE=n\nCONFIG_DEBUG=y\n')
        a = fixture.configuration_identity(self.output, 'aarch64', {'CONFIG_FILE': str(first)})
        b = fixture.configuration_identity(self.output, 'aarch64', {'CONFIG_FILE': str(second)})
        self.assertEqual(a, b)
        second.write_text('CONFIG_DEBUG=n\nCONFIG_TRACE=n\n')
        self.assertNotEqual(a, fixture.configuration_identity(self.output, 'aarch64', {'CONFIG_FILE': str(second)}))
        self.assertNotEqual(a, fixture.configuration_identity(self.output, 'aarch64',
                            {'CONFIG_FILE': str(first), 'CARGO_FEATURES': '--features kernel-test'}))

    def test_reject_wrong_commit_or_architecture(self):
        fixture.publish(self.root, self.output, 'aarch64', 'revision-a')
        for arch, revision in [('aarch64', 'revision-b'), ('riscv64', 'revision-a')]:
            with self.assertRaises(ValueError):
                fixture.verify(self.output, arch, revision)

    def test_reject_corrupt_missing_empty_or_symlinked_images(self):
        for name in fixture.FILES:
            for damage in ('corrupt', 'missing', 'empty', 'symlink'):
                with self.subTest(name=name, damage=damage):
                    fixture.publish(self.root, self.output, 'aarch64', 'revision-a')
                    path = self.output / name
                    if damage == 'corrupt':
                        path.write_bytes(b'corrupt')
                    elif damage == 'empty':
                        path.write_bytes(b'')
                    else:
                        path.unlink()
                        if damage == 'symlink':
                            path.symlink_to(self.root / 'target/app/aarch64/initramfs.cpio')
                    with self.assertRaises(ValueError):
                        fixture.verify(self.output, 'aarch64', 'revision-a')
                    path.unlink(missing_ok=True)

    def test_manifest_cannot_select_arbitrary_paths(self):
        fixture.publish(self.root, self.output, 'aarch64', 'revision-a')
        path = self.output / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['files']['../unexpected'] = '0' * 64
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            fixture.verify(self.output, 'aarch64', 'revision-a')


if __name__ == '__main__':
    unittest.main()
