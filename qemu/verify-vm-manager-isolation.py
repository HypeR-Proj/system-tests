#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Inject business-manager failures while exercising real Native disk services.

Uses a previously built split deployment. Only temporary copies of its system
tree and a new test disk are modified; the supplied images remain unchanged.
"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = {'exit': 0, 'closed-reply': 1, 'invalid-reply': 2, 'configured-exit': 3}
CASES = ('absent', 'missing', *FIXTURES, 'no-config')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    for name in ('image', 'initramfs', 'board', 'system-root', 'sdk', 'output', 'mkfs-erofs'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--artifact', action='append', default=[], metavar='NAME=PATH')
    parser.add_argument('--cases', nargs='+', choices=CASES, default=CASES)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    # A temporary directory also makes repeated invocations independent of old
    # fault images. Logs survive under --output even when a case fails.
    with tempfile.TemporaryDirectory(prefix='manager-isolation-', dir=args.output) as temporary:
        work = Path(temporary)
        fixtures = {}
        for case in args.cases:
            if case not in FIXTURES:
                continue
            fixture = work / case
            subprocess.run([
                str(args.sdk / 'bin/hyper-clang'), '-std=c17', '-Wall', '-Wextra', '-Werror',
                f'-DVM_PROVISION_TEST={FIXTURES[case]}',
                str(ROOT / 'tests/native/vm-provisioning.c'), '-o', str(fixture),
            ], check=True)
            fixtures[case] = fixture
        system = work / 'system-root'
        shutil.copytree(args.system_root, system, symlinks=True)
        init = json.loads((system / 'etc/hyper/components.json').read_text())
        unit_directory = system / init['configuration_directory'].lstrip('/')
        original = {path.name: path.read_text() for path in unit_directory.glob('*.json')}
        policy_path = system / 'etc/hyper/startup.json'
        original_policy = policy_path.read_text()
        for case in args.cases:
            policy = json.loads(original_policy)
            units = {name: json.loads(text) for name, text in original.items()}
            manager = system / 'svc/vm-manager'
            if manager.exists():
                manager.unlink()
            if case == 'absent':
                del units['vm-manager.json']
                units['root.json']['children'] = [c for c in units['root.json']['children'] if c['name'] != 'vm-manager']
                policy['applications'] = [a for a in policy['applications'] if a['component'] != '/vm-manager']
            elif case == 'no-config':
                for application in policy['applications']:
                    if application['component'] == '/vm-manager':
                        application.pop('fleet', None)
                shutil.copy2(args.system_root / 'svc/vm-manager', manager)
            elif case in fixtures:
                shutil.copy2(fixtures[case], manager)
            policy_path.write_text(json.dumps(policy))
            for name in original:
                path = unit_directory / name
                if name in units:
                    path.write_text(json.dumps(units[name], indent=2) + '\n')
                else:
                    path.unlink(missing_ok=True)
            # Missing executable is intentional; verify runtime degradation
            # rather than sending the deliberately incomplete tree to a packer
            # which checks executable presence in the bootstrap archive.
            disk = work / 'disk.img'
            pack = [sys.executable, '-B', str(ROOT / 'scripts/pack-board-image.py'),
                    '--board', str(args.board), '--output', str(disk), '--replace',
                    '--system-root', str(system), '--mkfs-erofs', str(args.mkfs_erofs)]
            for artifact in args.artifact:
                pack.extend(['--artifact', artifact])
            subprocess.run(pack, check=True)
            subprocess.run([
                sys.executable, '-B', str(Path(__file__).with_name('verify-boot-system.py')),
                '--qemu', args.qemu, '--image', str(args.image),
                '--initramfs', str(args.initramfs), '--board', str(args.board),
                '--disk', str(disk), '--manager-case', case,
                '--log', str(args.output / f'{case}.log'),
            ], check=True)


if __name__ == '__main__':
    main()
