#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Exercise supervisor retention through real Native processes and channels."""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil

from hyper_test.command import Shell
from hyper_test.journal import Journal
from hyper_test.session import Session, native_command


def prepare(args):
    args.output.mkdir(parents=True, exist_ok=False)
    system = args.output / 'system-root'
    shutil.copytree(args.composition / 'system-root', system, symlinks=True)
    shutil.copytree(args.composition / 'native-system', args.output / 'native-system')
    declarations = system / 'etc/components'
    root_path = declarations / 'root.json'
    root = json.loads(root_path.read_text())
    root['children'].append({'name': 'retirement-probe', 'component': 'retirement-probe'})
    root_path.write_text(json.dumps(root))
    (declarations / 'retirement-probe.json').write_text(json.dumps({
        'name': 'retirement-probe',
        'program': {'image': '/bin/retirement-probe', 'prepared': True},
    }))
    policy_path = system / 'etc/hyper/startup.json'
    policy = json.loads(policy_path.read_text())
    capabilities = []
    for source, purpose, rights in (
        ('task-factory', 'task-factory', ['create-process', 'create-task-group', 'retain-process']),
        ('resource-domain', 'resource-domain', ['sponsor']),
        ('root-directory', 'root-directory', ['read', 'execute', 'protect', 'inspect']),
    ):
        capabilities.append({'source': 'bootstrap.' + source, 'purpose': 'process.' + purpose,
                             'operation': 'duplicate', 'rights': rights + ['duplicate', 'transfer']})
    policy['applications'].append({'component': '/retirement-probe', 'capabilities': capabilities})
    policy_path.write_text(json.dumps(policy))
    spec = importlib.util.spec_from_file_location(
        'fixture', Path(__file__).with_name('prepare-native-fixture.py'))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    image = args.output / 'retirement.cpio'
    fixture.publish(args.output.resolve(), image.resolve(), args.packer, args.strip)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('composition', 'output', 'image'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('packer', 'strip', 'qemu'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    initramfs = prepare(args)
    log = args.output / 'serial.log'
    with Session(native_command(args.qemu, args.image, initramfs), log) as vm:
        boot = vm.await_text(rb'hyper-sh\$ |fallback-sh\$ |HypeR: startup incomplete', timeout=180)
        if not boot.endswith(b'hyper-sh$ '):
            raise AssertionError(f'Native startup failed before retirement probe: {boot[-4096:]!r}')
        shell = Shell(vm)
        journal = Journal(shell)
        for marker in (
            'AUTHORITY', 'MEMORY_REGISTERS_AND_PEER_CLOSED',
            'RELEASE_REVOKES_ALL_VIEWS', 'OWNER_LOSS_AND_FAULT', 'DEADLINE', 'MEMORY_LIMIT',
            'CONCURRENT_LAST_EXIT',
        ):
            journal.message('/retirement-probe', 'RETIREMENT_' + marker + '_OK')
        shell.check('echo RETIREMENT_SUPERVISOR_ALIVE')
    print(f'process retirement acceptance passed: {log}')


if __name__ == '__main__':
    main()
