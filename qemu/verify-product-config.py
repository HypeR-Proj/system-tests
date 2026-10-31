#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Boot the trimmed product and inspect live services through captured commands."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from hyper_test.command import Shell
from hyper_test.journal import Journal
from hyper_test.session import Session

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from native_components import load_registry
import system_config
sys.path.insert(0, str(ROOT / 'tests/build'))
from product_fixture import trimmed_product, write_product


def prepare(output):
    """Generate a test-only selection from the scenario's required programs."""
    entries = load_registry(ROOT / 'mk/components.mk')['entries']
    source = trimmed_product(entries, reporter=True)
    system_config.ProductConfig(source).select(entries)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_product(output, source)


def program_inventory(output, expected, directory):
    actual = output.decode().strip().splitlines()
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise AssertionError(f'{directory}: expected {sorted(expected)}, observed {actual}')


def service_inventory(output):
    for name in ('console-raw', 'io-runtime', 'fs-backend', 'journal', 'session-manager'):
        if re.search(rb'(?m)^/' + name.encode() + rb'\s+running\s', output) is None:
            raise AssertionError(f'{name}: not running in component snapshot: {output!r}')
    if re.search(rb'(?m)^/vm-manager\s', output):
        raise AssertionError('excluded VM manager has a runtime component')
    if b'bootstrap-retired' not in output:
        raise AssertionError(f'bootstrap retirement missing from component state: {output!r}')


def run(args):
    product = system_config.load(args.config)
    entries = product.select(load_registry(ROOT / 'mk/components.mk')['entries'])
    programs = [entry for entry in entries if 'binary' in entry
                and entry.get('environment') == 'system']
    names = {entry['staged'] for entry in programs}
    required = {'test-report', 'sh', 'componentctl', 'journalctl', 'ls', 'cat', 'free', 'ps', 'mount-system'}
    if not required <= names or {'fs-fat-system', 'vm-manager', 'vm-runtime', 'vmm'} & names:
        raise ValueError('product runtime test requires the trimmed fixture and test-report')
    args.log.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-B', str(ROOT / 'scripts/run-io-vm.py'), '--qemu', args.qemu,
               '--image', str(args.image), '--initramfs', str(args.initramfs),
               '--disk', str(args.disk), '--board', str(args.board)]
    evidence = {'status': 'running', 'configuration': product.value,
                'configuration_id': product.identity(), 'command': command,
                'sha256': {name: hashlib.sha256(path.read_bytes()).hexdigest()
                           for name, path in (('image', args.image), ('initramfs', args.initramfs),
                                              ('board', args.board))}}
    record = args.log.with_suffix('.json')
    record.write_text(json.dumps(evidence, indent=2) + '\n')
    with Session(command, args.log) as vm:
        boot = vm.await_text(rb'hyper-sh\$ |fallback-sh\$ |HypeR: startup incomplete', timeout=240)
        if not boot.endswith(b'hyper-sh$ '):
            raise AssertionError(f'configured product did not enter formal shell: {boot[-4096:]!r}')
        shell = Shell(vm)
        deadline = time.monotonic() + 60
        while True:
            components = shell.check('componentctl list', timeout=60)
            if b'bootstrap-retired' in components:
                break
            if time.monotonic() >= deadline:
                raise AssertionError(f'bootstrap did not retire: {components!r}')
            vm.pump(0.1)
        service_inventory(components)
        for directory in ('bin', 'svc'):
            expected = {entry['destination'].split('/', 1)[1] for entry in programs
                        if entry['destination'].startswith(directory + '/')}
            program_inventory(shell.check(f'ls -1 /{directory}'), expected, directory)
        processes = shell.check('ps --no-headers')
        if re.search(rb'\b(?:fs-fat|vm-manager|bootstrap-stage1)\b', processes):
            raise AssertionError(f'excluded or retired process remains: {processes!r}')
        if b'fs-erofs' not in processes or b'io-runtime' not in processes:
            raise AssertionError(f'live root providers missing: {processes!r}')
        mounts = shell.check('mount')
        if b' on / type erofs (ro)' not in mounts or b' on /data ' in mounts:
            raise AssertionError(f'expected independent EROFS root without data mount: {mounts!r}')
        if shell.check('cat /etc/fstab').strip() or shell.check('ls -1 /boot').strip():
            raise AssertionError('formal fstab is not empty or bootstrap archive remains attached')
        expected = b'CONFIGURED_PRODUCT_TMP_OK'
        output = shell.check("printf 'CONFIGURED_PRODUCT_TMP_OK\\n' > /tmp/product-proof; cat /tmp/product-proof")
        if output.strip() != expected:
            raise AssertionError(f'RAM temporary storage readback failed: {output!r}')
        shell.check('free')
        Journal(shell).message('/bootstrap-retire', 'bootstrap retired; boot archive detached')
        evidence.update(status='passed', commands=str(shell.artifacts),
                        deployed_programs=sorted(names), root_filesystem='erofs', data_mounted=False)
    record.write_text(json.dumps(evidence, indent=2) + '\n')
    print(f'configured product boot, retirement and executable closure passed: {args.log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    prepare_parser = actions.add_parser('prepare')
    prepare_parser.add_argument('--output', type=Path, required=True)
    run_parser = actions.add_parser('run')
    run_parser.add_argument('--qemu', required=True)
    run_parser.add_argument('--system-config', '--config', dest='config', type=Path, required=True)
    for name in ('image', 'initramfs', 'disk', 'board', 'log'):
        run_parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.output)
    else:
        run(args)


if __name__ == '__main__':
    main()
