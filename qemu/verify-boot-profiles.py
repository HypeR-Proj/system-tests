#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Select real stage-1 profiles with a test-only independent RAM root.

No physical block driver or I/O VM participates. Board acceptance separately
exercises the default storage-backed profile and live provider adoption.
"""

import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

from hyper_test.command import Shell
from hyper_test.guest_console import append_console_output
from hyper_test.session import Session, native_command

ROOT = Path(__file__).resolve().parents[2]
PROFILE = 'etc/hyper/boot-profiles/'


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def service_fixture(args, entries, default):
    """Add one non-VM provider with an implementation-private migration record."""
    records = {name: source for _, name, source in entries}
    manifest = json.loads(Path(records[default['services'].lstrip('/')]).read_text())
    manifest['services'].append({
        'name': 'profile-storage-old', 'image': '/svc/profile-storage-old',
        'critical': True, 'restart': 'never', 'after': ['console-raw'],
        'loader': 'bootstrap', 'capabilities': [{
            'source': 'bootstrap.io-ready-channel', 'purpose': 'io.ready',
            'operation': 'move', 'rights': ['wait', 'write'],
        }],
    })
    manifest_path = args.output / 'service-manifest.json'
    write_json(manifest_path, manifest)
    profile = copy.deepcopy(default)
    profile['services'] = '/etc/hyper/profile-service.json'
    profile['io'] = {'kind': 'service', 'service': 'profile-storage-old',
                     'successor': '/profile-storage'}
    profile_path = args.output / 'service.json'
    write_json(profile_path, profile)
    entries.extend([
        ['0644', 'etc/hyper/profile-service.json', str(manifest_path.resolve())],
        ['0644', PROFILE + 'service.json', str(profile_path.resolve())],
        ['0755', 'svc/profile-storage-old', str(args.probe.resolve())],
    ])

    # The same static probe has separate predecessor/successor processes. Copy
    # the formal tree rather than mutating the shared native fixture output.
    system = args.output / 'system-root'
    if system.exists():
        shutil.rmtree(system)
    shutil.copytree(args.entries.parent / 'system-root', system, symlinks=True)
    shutil.copy2(args.probe, system / 'svc/profile-storage-new')
    root_path = system / 'etc/components/root.json'
    root = json.loads(root_path.read_text())
    root['children'].append({'name': 'profile-storage', 'component': 'profile-storage'})
    write_json(root_path, root)
    write_json(system / 'etc/components/profile-storage.json', {
        'name': 'profile-storage', 'program': {'image': '/svc/profile-storage-new',
        'prepared': True, 'resident': True, 'critical': True},
    })
    policy_path = system / 'etc/hyper/startup.json'
    policy = json.loads(policy_path.read_text())
    policy['providers'].append({'role': 'storage', 'component': '/profile-storage'})
    write_json(policy_path, policy)
    records = []
    for path in sorted(system.rglob('*')):
        name = path.relative_to(system).as_posix()
        if path.is_symlink():
            records.extend(['symlink', name, path.readlink().as_posix()])
        elif path.is_file():
            records.extend([f'0{path.stat().st_mode & 0o777:03o}', name, str(path.resolve())])
    payload = args.output / 'test-system.cpio'
    with payload.open('wb') as stream:
        subprocess.run([args.packer, *records], stdout=stream, check=True)
    entries[:] = [entry for entry in entries if entry[1] != 'test-system.cpio']
    entries.append(['0644', 'test-system.cpio', str(payload.resolve())])


def prepare(args):
    entries = json.loads(args.entries.read_text())
    records = {name: source for _, name, source in entries}
    default = json.loads(Path(records[PROFILE + 'default.json']).read_text())
    if default['io'] != {'kind': 'none'} or 'handoff' not in default:
        raise ValueError('profile fixture requires a no-storage independent-root deployment')
    args.output.mkdir(parents=True, exist_ok=True)
    service_fixture(args, entries, default)
    for name in ('alternate', 'recovery'):
        profile = copy.deepcopy(default)
        if name == 'recovery':
            del profile['handoff']
        path = args.output / (name + '.json')
        path.write_text(json.dumps(profile, indent=2) + '\n')
        entries.append(['0644', PROFILE + name + '.json', str(path.resolve())])
    manifest = args.output / 'entries.json'
    manifest.write_text(json.dumps(entries, indent=2) + '\n')
    archive = args.output / 'bootstrap.cpio'
    subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/pack-native-initramfs.py'),
                    '--packer', args.packer, '--strip', args.strip, '--output', str(archive),
                    '--entries-from', str(manifest)], check=True)
    return archive


def run(args, archive, selector, name, *, reject=False, recovery=False):
    command = native_command(args.qemu, args.image, archive)
    index = command.index('-append') + 1
    command[index] += (' ' + selector) if selector else ''
    remaining = b'' if name == 'default' else b'probe=one probe=two unknown-flag value=a=b'
    if not reject and not recovery:
        command[index] += ' nokaslr ' + remaining.decode()
    log = args.output / (name + '.log')
    with Session(command, log, output_filter=append_console_output) as session:
        if reject:
            expected = (rb'/etc/hyper/boot-profiles/missing\.json:' if name == 'missing'
                        else rb'boot profile selector:')
            session.await_text(expected, timeout=120)
            # Errors must happen before launching any graph, not after a
            # fallback profile has already started privileged services.
            session.pump(0.1)
            evidence = log.read_bytes()
            for forbidden in (b'boot profile:', b'console-raw: ready',
                              b'HypeR component manager', b'IO VM:'):
                if forbidden in evidence:
                    raise AssertionError(f'rejected profile started services: {forbidden!r}')
        else:
            selected = b'default' if not selector else name.encode()
            session.await_text(rb'boot profile: ' + selected + rb'\n', timeout=120)
            if recovery:
                session.await_text(rb'fallback-sh\$ ', timeout=120)
                session.send(b'help\n')
                session.await_text(rb'Recovery shell:', timeout=30)
                session.await_text(rb'fallback-sh\$ ', timeout=30)
                if b'core supervision adopted' in log.read_bytes():
                    raise AssertionError('recovery-only profile handed off supervision')
            else:
                if remaining:
                    session.await_text(
                        rb'HypeR component-manager: unrecognized boot arguments: ' + remaining + rb'\n',
                        timeout=120,
                    )
                session.await_text(rb'HypeR: startup complete', timeout=180)
                session.await_text(rb'hyper-sh\$ ', timeout=30)
                shell = Shell(session)
                shell.check('ls /boot/init', failed=True, timeout=30)
                processes = shell.check('ps --no-headers', timeout=30)
                if b'io-runtime' in processes or b'fs-backend' in processes:
                    raise AssertionError(f'unselected storage provider launched: {processes!r}')
                if name != 'service' and b'profile-storage' in processes:
                    raise AssertionError(f'unused replacement candidate launched: {processes!r}')
                if name == 'service':
                    if b'profile-storage' not in processes or b'profile-storage-old' in processes:
                        raise AssertionError(f'provider was not replaced: {processes!r}')
                    journal = shell.check('journalctl -n 100', timeout=30)
                    if b'PROFILE_STORAGE_OWNER_ADOPTED' not in journal:
                        raise AssertionError(f'opaque owner adoption missing: {journal!r}')
                    components = shell.check('componentctl list', timeout=30)
                    if not any(b'/profile-storage' in line and b'running' in line
                               for line in components.splitlines()):
                        raise AssertionError(f'storage provider not supervised: {components!r}')
                if b'bootstrap retired; boot archive detached' not in log.read_bytes():
                    raise AssertionError('no-I/O-VM profile did not retire bootstrap')
                reports = [line for line in log.read_bytes().splitlines()
                           if b'unrecognized boot arguments:' in line]
                if len(reports) != (1 if remaining else 0):
                    raise AssertionError(f'unexpected boot-argument reports: {reports!r}')
    print(f'Boot profile {name} passed: {log}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    for name in ('image', 'entries', 'output', 'probe'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('packer', 'strip'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    archive = prepare(args)
    run(args, archive, '', 'default')
    run(args, archive, 'hyper.boot-profile=alternate', 'alternate')
    run(args, archive, 'hyper.boot-profile=service', 'service')
    run(args, archive, 'hyper.boot-profile=recovery', 'recovery', recovery=True)
    run(args, archive, 'hyper.boot-profile=missing', 'missing', reject=True)
    run(args, archive, 'hyper.boot-profile=alternate hyper.boot-profile=default', 'duplicate', reject=True)


if __name__ == '__main__':
    main()
