#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Observe mounted worker death without losing its parent or unrelated mounts.

The disposable disk boots the normal component manager, fs-backend and EROFS
root. Only the FAT worker executable is replaced by a test provider: it retains
the volume capability, never writes media, and exits/faults during a write RPC.
"""

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from hyper_test.command import Shell
from hyper_test.journal import Journal
from hyper_test.session import Session

ROOT = Path(__file__).resolve().parents[2]


def prepare(args):
    args.output.mkdir(parents=True, exist_ok=False)
    system = args.output / 'system-root'
    shutil.copytree(args.system_root, system, symlinks=True)
    shutil.copy2(args.worker, system / 'svc/fs-fat')
    shutil.copy2(args.reporter, system / 'bin/test-report')
    command = [sys.executable, '-B', str(ROOT / 'scripts/pack-board-image.py'),
               '--board', str(args.board), '--output', str(args.output / 'disk.img'),
               '--system-root', str(system), '--mkfs-erofs', str(args.mkfs_erofs)]
    for artifact in args.artifact:
        command.extend(['--artifact', artifact])
    subprocess.run(command, check=True)


def run(args):
    command = [sys.executable, '-B', str(ROOT / 'scripts/run-io-vm.py'),
               '--qemu', args.qemu, '--image', str(args.image),
               '--initramfs', str(args.initramfs), '--disk', str(args.disk),
               '--board', str(args.board)]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with Session(command, args.log) as vm:
        vm.await_text(rb'hyper-sh\$ ', timeout=180)
        shell = Shell(vm)
        journal = Journal(shell)
        # This also checks expected stop/restart does not poison component health.
        shell.check('ls /data')
        shell.check('umount /data', timeout=60)
        shell.check('mount /data', timeout=60)
        healthy = shell.check('componentctl status /fs-backend')
        if b'failed' in healthy.lower():
            raise AssertionError(f'clean unmount reported a failed worker: {healthy!r}')
        shell.check('ls /')
        # The provider emits no error itself. Its owner must observe the kernel's
        # terminal reason, even with no subsequent mount-management request.
        if args.case == 'stop-fault':
            shell.check('echo fault >/data/stop-fault')
            shell.check('umount /data', failed=True, timeout=30)
        else:
            shell.check(f'echo fault >/data/{args.case}', failed=True, timeout=30)
        reason = 'ProcessExited { status: 37 }' if args.case == 'exit' else 'Fault {'
        vm.observe(rb'HypeR component-manager: CRITICAL: /fs-backend generation=1 worker failure: .*'
                   + re.escape(reason.encode()), 15)
        records = journal.message('/fs-backend', reason, stream='stderr')
        failures = [r for r in records if reason in r['text']]
        if len(failures) != 1 or not all(s in failures[0]['text'] for s in ('fs-fat', '/data', '0x')):
            raise AssertionError(f'missing or repeated worker identity/reason: {failures!r}')
        until = time.monotonic() + 15
        while True:
            state = shell.check('componentctl status /fs-backend')
            if reason.encode() in state and b'/data' in state:
                break
            if time.monotonic() >= until:
                raise AssertionError(f'worker failure missing from component status: {state!r}')
            vm.pump(0.1)
        if not re.search(rb'/fs-backend\s+running\s', state):
            raise AssertionError(f'worker failure stopped its owner: {state!r}')
        if args.case == 'stop-fault':
            # The namespace transaction committed before shutdown failed. The
            # lost media capability cannot be manufactured by mounting again.
            shell.check('mount /data', failed=True)
            shell.check('ls /')
            shell.check('ls /bin')
            final = shell.check('componentctl status /fs-backend')
            if reason.encode() not in final:
                raise AssertionError(f'owner release erased worker failure: {final!r}')
            print(f'Filesystem shutdown failure reporting passed: {args.log}')
            return
        mounts = shell.check('mount')
        if b'/data' not in mounts or reason.encode() not in mounts:
            raise AssertionError(f'mount lost worker failure record: {mounts!r}')
        for command in ('ls /', 'ls -S /', 'ls -t /'):
            listing = shell.check(command, failed=True)
            for pattern in (rb'\?{10}\s+\?\s+data\n', rb'\sbin/\n', rb'\setc/\n', rb'ls: /data:'):
                if not re.search(pattern, listing):
                    raise AssertionError(f'{command}: lost healthy or failed entry: {listing!r}')
        shell.check('ls /bin')
        shell.check('cat /etc/fstab')
        shell.check('echo ROOT_REMAINS_WRITABLE >/tmp/worker-failure-check')
        if b'ROOT_REMAINS_WRITABLE' not in shell.check('cat /tmp/worker-failure-check'):
            raise AssertionError('healthy tmp mount lost write/read service')
        shell.check('ls /data', failed=True)
        shell.check('touch /data/new-file', failed=True)
        shell.check('mount -a', failed=True)
        final = shell.check('componentctl status /fs-backend')
        if reason.encode() not in final:
            raise AssertionError(f'failed retry erased terminal status: {final!r}')
    print(f'Filesystem worker {args.case} supervision and mount isolation passed: {args.log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('prepare')
    for name in ('system-root', 'board', 'output', 'mkfs-erofs', 'worker', 'reporter'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--artifact', action='append', default=[])
    p = sub.add_parser('run')
    p.add_argument('--case', choices=('exit', 'fault', 'stop-fault'), required=True)
    p.add_argument('--qemu', required=True)
    for name in ('image', 'initramfs', 'disk', 'board', 'log'):
        p.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    {'prepare': prepare, 'run': run}[args.mode](args)


if __name__ == '__main__':
    main()
