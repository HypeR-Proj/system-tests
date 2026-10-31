#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Measure interactive Native command latency on local AArch64 QEMU TCG."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qemu'))
from hyper_test.session import Session, native_command
from hyper_test.shell_status import checked, status

ROOT = Path(__file__).resolve().parents[2]
_runner_spec = importlib.util.spec_from_file_location('board_runner', ROOT / 'scripts/run-io-vm.py')
_runner = importlib.util.module_from_spec(_runner_spec)
_runner_spec.loader.exec_module(_runner)


DEFAULT_COMMANDS = (
    'pwd',
    'ls -d /bin/ls',
    'free --bytes',
    'ls -1 /bin',
)


def identity(path):
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'path': str(path.resolve()), 'bytes': path.stat().st_size, 'sha256': digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', default='qemu-system-aarch64')
    parser.add_argument('--board', type=Path, default=ROOT / 'boards/qemu.json')
    for name in ('image', 'initramfs', 'disk', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--boots', type=int, default=3)
    parser.add_argument('--repeat', type=int, default=10,
                        help='repeat samples after the first invocation in each boot')
    parser.add_argument('--command', action='append', dest='commands',
                        help='override the default commands (may be repeated)')
    parser.add_argument('--ready', default='HypeR vm-manager: ready',
                        help='boot marker to await before synchronizing the shell prompt')
    args = parser.parse_args()
    if any(os.environ.get(name, '').lower() == 'true' for name in ('CI', 'GITHUB_ACTIONS')):
        parser.error('performance measurement is local/manual only')
    if args.boots < 1 or args.repeat < 1:
        parser.error('--boots and --repeat must be positive')
    if args.output.exists():
        parser.error('output already exists; choose a new directory to preserve earlier results')
    for path in (args.image, args.initramfs, args.disk):
        if not path.is_file():
            parser.error(f'missing artifact: {path}')
    args.output.mkdir(parents=True)
    sys.path.insert(0, str(ROOT / 'scripts'))
    board = _runner.validate_board_disk(args.disk, args.board)
    command = native_command(args.qemu, args.image.resolve(), args.initramfs.resolve())
    machine_index = command.index('-machine') + 1
    command[machine_index] = _runner.machine_arguments(board, command[machine_index])
    command += [
        '-accel', 'tcg,thread=multi', '-global', 'virtio-mmio.force-legacy=false',
        '-drive', 'if=none,id=physicaldisk,format=raw,snapshot=on,cache=writeback,file='
        + str(args.disk.resolve()).replace(',', ',,'),
        '-device', _runner.storage_controller(board),
        '-device', 'scsi-hd,drive=physicaldisk,bus=physicalscsi.0,scsi-id=0,lun=0',
    ]
    command.extend(_runner.network_arguments(board))
    report = {
        'host': platform.platform(),
        'qemu_version': subprocess.check_output([args.qemu, '--version'], text=True),
        'qemu_command': command,
        'artifacts': {name: identity(getattr(args, name))
                      for name in ('image', 'initramfs', 'disk')},
        'metric': 'host command write to returned shell prompt, milliseconds',
        'limits': [
            'Includes command execution, output delivery, and foreground process cleanup.',
            'First invocation is after normal boot; shared libraries may already be cached.',
            'Guest caches restart each boot; host disk cache is uncontrolled.',
            'TCG values do not predict physical hardware performance.',
            'The disk uses a temporary QEMU snapshot and is not modified.',
        ],
        'runs': [],
    }
    result_path = args.output / 'summary.json'

    def save():
        result_path.write_text(json.dumps(report, indent=2) + '\n')

    save()
    for boot in range(args.boots):
        with Session(command, args.output / f'boot-{boot + 1}.log') as session:
            session.await_text(args.ready.encode(), timeout=240)
            session.pump(1)
            session.pending.clear()
            session.send(b'\n')
            session.await_text(rb'hyper-sh\$ ')
            for text in args.commands or DEFAULT_COMMANDS:
                samples = []
                for _ in range(args.repeat + 1):
                    started = time.monotonic_ns()
                    session.send(text.encode() + b'\n')
                    output = session.await_text(rb'hyper-sh\$ ', timeout=120)
                    samples.append((time.monotonic_ns() - started) / 1_000_000)
                    # Check the exit status after stopping the timer, so the
                    # result probe does not inflate the command-latency sample.
                    session.send(checked('test $? -eq 0'))
                    result = session.await_text(rb'hyper-sh\$ ', timeout=120)
                    if status(result) != 0:
                        raise RuntimeError(f'{text}: command failed: {output!r}')
                row = {'boot': boot + 1, 'command': text, 'first_ms': samples[0],
                       'repeated_ms': samples[1:],
                       'repeated_median_ms': statistics.median(samples[1:])}
                report['runs'].append(row)
                save()
                print(f"boot {boot + 1} {text}: first {samples[0]:.2f} ms; "
                      f"repeat median {row['repeated_median_ms']:.2f} ms", flush=True)
    print(f'Results: {result_path}')


if __name__ == '__main__':
    main()
