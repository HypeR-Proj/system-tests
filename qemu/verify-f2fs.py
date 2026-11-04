#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
"""Verify a prepared disposable F2FS board across a complete QEMU restart."""
import argparse
from pathlib import Path
import sys
from hyper_test.session import Session
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from hyper_test.shell_status import checked, status


def boot(args, mode, *, clean=True, label=None):
    command = [sys.executable, str(ROOT / 'scripts/run-io-vm.py'), '--qemu', args.qemu,
               '--image', str(args.image), '--initramfs', str(args.initramfs),
               '--disk', str(args.disk), '--board', str(args.board)]
    log = args.log.with_suffix(f'.{label or mode}.log')
    with Session(command, log) as session:
        session.await_text(rb'hyper-sh\$ ', timeout=180)
        session.send(checked('mount'))
        mounts = session.await_text(rb'hyper-sh\$ ')
        if status(mounts) != 0 or b'type f2fs (rw)' not in mounts:
            raise RuntimeError(f'F2FS mount missing: {mounts!r}')
        session.send(checked('cat /data/vms.json'))
        seeded = session.await_text(rb'hyper-sh\$ ')
        if status(seeded) != 0 or b'virtual-machines' not in seeded:
            raise RuntimeError(f'production-seeded configuration missing: {seeded!r}')
        session.send(checked(f'/bin/f2fs-smoke {mode} /data'))
        result = session.await_text(rb'hyper-sh\$ ', timeout=180)
        if status(result) != 0 or f'F2FS-SMOKE: {mode} PASS'.encode() not in result:
            session.send(checked('journalctl -n 60'))
            journal = session.await_text(rb'hyper-sh\$ ')
            raise RuntimeError(f'F2FS {mode} failed: {result!r}; journal: {journal!r}')
        if clean:
            session.send(checked('umount /data'))
            result = session.await_text(rb'hyper-sh\$ ', timeout=120)
            if status(result) != 0:
                raise RuntimeError(f'F2FS unmount failed: {result!r}')
    print(f'F2FS I/O VM {mode}: PASS ({log})', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', default='qemu-system-aarch64')
    for name in ['image', 'initramfs', 'disk', 'board', 'log']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    for mode in ['write', 'verify']:
        boot(args, mode)
    # Terminate QEMU with the filesystem mounted after sync was acknowledged.
    boot(args, 'write', clean=False, label='checkpoint-write')
    boot(args, 'verify', label='checkpoint-verify')
    boot(args, 'writeback')

    boot(args, 'stream-write')
    boot(args, 'stream-verify')

    for mode in ['periodic-idle', 'periodic-busy']:
        boot(args, mode, clean=False)
        boot(args, 'periodic-verify', label=mode + '-verify')
