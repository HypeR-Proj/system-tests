#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Publish a synthetic no-device Native root fixture, never a product deployment.

The ordinary composer writes separate boot and formal trees. This test publisher
adds an opaque payload that the explicitly selected test init copies into a new
RAMFS mount before entering the real bootstrap supervisor. It does not exercise
block storage or boot latency; independent board tests cover physical providers.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def publish(composition, output, packer, strip):
    system = composition / 'system-root'
    records = []
    for path in sorted(system.rglob('*')):
        name = path.relative_to(system).as_posix()
        if path.is_symlink():
            records.extend(['symlink', name, path.readlink().as_posix()])
        elif path.is_file():
            records.extend([f'0{path.stat().st_mode & 0o777:03o}', name, str(path.resolve())])
        elif not path.is_dir():
            raise ValueError(f'unsupported fixture file: {path}')
    if not records:
        raise ValueError('independent fixture root is empty')
    payload = composition / 'test-system.cpio'
    with payload.open('wb') as stream:
        subprocess.run([packer, *records], stdout=stream, check=True)
    entries = json.loads((composition / 'native-system/boot.entries.json').read_text())
    # A synthetic independent root is already mounted by the test init. Do not
    # pretend a GPT device or storage worker mounted it from a physical disk.
    fstab = composition / 'fixture-boot-fstab'
    fstab.write_text('')
    entries = [record for record in entries if record[1] not in ('etc/fstab', 'test-system.cpio')]
    entries.extend([['0644', 'etc/fstab', str(fstab.resolve())],
                    ['0644', 'test-system.cpio', str(payload.resolve())]])
    manifest = composition / 'fixture.entries.json'
    manifest.write_text(json.dumps(entries, indent=2) + '\n')
    subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/pack-native-initramfs.py'),
                    '--packer', packer, '--strip', strip, '--output', str(output),
                    '--entries-from', str(manifest)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--composition', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--packer', required=True)
    parser.add_argument('--strip', required=True)
    args = parser.parse_args()
    publish(args.composition.resolve(), args.output.resolve(), args.packer, args.strip)


if __name__ == '__main__':
    main()
