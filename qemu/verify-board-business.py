#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
"""Prepare and exercise an ordinary 128 MiB VM backed by a board disk volume.

The verified external appliance owns the Linux test implementation. Its
hyper.test=hold mode keeps the completed test alive for Native stop/restart.
Preparation needs only downloaded package artifacts, never a Linux checkout.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time

from hyper_test.shell_status import checked, status as command_status

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from board_config import Board


def prepare(args):
    spec = importlib.util.spec_from_file_location('hyper_io_images', ROOT / 'scripts/io-vm-images.py')
    images = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(images)
    kernel, ramfs = images.package_payloads(args.package)
    args.output.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(args.fit_pack.resolve()), str(args.output / 'business.itb'), 'arm64',
                    str(kernel), '0x40200000', '0x40200000', str(ramfs)], check=True)
    board = json.loads((ROOT / 'boards/qemu.json').read_text())
    board['io-vm'].pop('network-device', None)
    board['io-vm'].pop('networks', None)
    board['files'].pop('vm/alpine.itb')
    board['files']['vm/business.itb'] = 'business'
    board['disk']['partitions'] = [
        part for part in board['disk']['partitions'] if 'content' in part
    ] + [{'label': 'business', 'size-mib': 8}]
    board['virtual-machines'] = [{'name': 'business', 'image': 'vm/business.itb',
                                  'autostart': False, 'disk': {
                                      'source': 'PARTLABEL=business',
                                      'backend': {'request-queues': 1, 'sg-prealloc': 32}},
                                  'configuration': {'vcpus': 1,
                                                    'memory-bytes': 128 * 1024 * 1024,
                                                    'bootargs': 'console=ttyAMA0 earlycon rdinit=/init loglevel=4 hyper.role=business hyper.test=hold'}}]
    config = args.output / 'config.json'
    config.write_text(json.dumps(board, indent=2) + '\n')
    Board.load(config)
    print(f'Prepared 128 MiB ordinary VM: {args.output / "business.itb"}')
    print(f'Board config: {config}; pack artifact business={args.output / "business.itb"}')


def run(args):
    board = Board.load(args.board)
    volume = next(part for part in board.partitions if part.name == 'business')
    command = [sys.executable, '-B', str(ROOT / 'scripts/run-io-vm.py'), '--qemu', args.qemu,
               '--image', str(args.image), '--initramfs', str(args.initramfs),
               '--disk', str(args.disk), '--board', str(args.board)]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open('wb') as log, selectors.DefaultSelector() as selector:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        selector.register(child.stdout, selectors.EVENT_READ)
        pending = bytearray()

        def drain_diagnostics():
            limit = time.monotonic() + 3
            while time.monotonic() < limit:
                for key, _ in selector.select(0.1):
                    data = os.read(key.fd, 65536)
                    if data:
                        log.write(data); log.flush()
                        pending.extend(data.replace(b'\r', b''))

        def await_text(pattern, timeout=180):
            limit = time.monotonic() + timeout
            while time.monotonic() < limit:
                match = re.search(pattern, pending)
                if match:
                    result = bytes(pending[:match.end()]); del pending[:match.end()]
                    return result
                if child.poll() is not None:
                    raise RuntimeError(f'QEMU exited: {child.returncode}')
                for key, _ in selector.select(0.1):
                    data = os.read(key.fd, 65536)
                    if not data:
                        raise RuntimeError('QEMU console closed')
                    log.write(data); log.flush(); pending.extend(data.replace(b'\r', b''))
                    if any(marker in pending for marker in (b'Kernel panic', b'HypeR KERNEL PANIC',
                            b'bootstrap failed', b'business disk: FAIL', b'quiescence failed',
                            b'vmm:', b'sh: command failed')):
                        drain_diagnostics()
                        raise RuntimeError(f'guest/storage failure: {bytes(pending[-4096:])!r}')
            raise TimeoutError(f'waiting for {pattern!r}: {bytes(pending[-4096:])!r}')

        def send(line):
            child.stdin.write(line); child.stdin.flush()

        def shell(line):
            send(checked(line))
            result = await_text(rb'(?m)^HYPER-COMMAND-STATUS=[0-9]+\n')
            await_text(rb'hyper-sh\$ ')
            if command_status(result) != 0:
                raise RuntimeError(f'command failed: {result!r}')
            return result

        def verify_guest():
            limit = time.monotonic() + 90
            while True:
                status = shell('vmm status scatter-smoke')
                if re.search(rb'scatter-smoke\s+running\b', status):
                    resident = re.search(rb'allocated VM backing: ([0-9]+) bytes', status)
                    capacity = re.search(rb'RAM capacity: ([0-9]+) MiB', status)
                    if resident and capacity:
                        if not 0 < int(resident[1]) <= int(capacity[1]) * 1024 * 1024:
                            raise RuntimeError(f'Invalid allocated VM backing: {status!r}')
                        break
                if re.search(rb'scatter-smoke\s+(failed|stopped)\b', status):
                    drain_diagnostics()
                    raise RuntimeError(f'VM failed before console attach: {status!r}')
                if time.monotonic() >= limit:
                    drain_diagnostics()
                    raise TimeoutError(f'VM did not reach running state: {status!r}')
                time.sleep(0.2)
            send(b'vmm console scatter-smoke\n')
            await_text(rb'HypeR business disk: acceptance complete\n')
            send(b'\x1dd')
            await_text(rb'hyper-sh\$ ')

        def stopped():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if re.search(rb'scatter-smoke\s+stopped\b', shell('vmm status scatter-smoke')):
                    return
                time.sleep(0.2)
            raise TimeoutError('ordinary VM did not reach stopped state')

        try:
            await_text(rb'\[HypeR bootstrap stage 1\]\[io-runtime\] ready; bootstrap mounts complete\n')
            await_text(rb'hyper-sh\$ ')
            # READY precedes init provisioning. Wait for its stopped definition
            # before replacing it with the CLI-created test VM; both cannot own
            # the same volume concurrently.
            deadline = time.monotonic() + 60
            while not re.search(rb'\bbusiness\s+stopped\b', shell('vmm list')):
                if time.monotonic() >= deadline:
                    raise TimeoutError('init did not provision the board VM')
                time.sleep(0.2)
            shell('vmm delete business')
            shell('vmm create scatter-smoke --config /data/vms.json --from business')
            shell('vmm start scatter-smoke'); verify_guest()
            shell('vmm stop scatter-smoke'); stopped()
            shell('vmm start scatter-smoke'); verify_guest()
            shell('vmm restart scatter-smoke'); verify_guest()
            shell('vmm stop scatter-smoke'); stopped()
            shell('echo BOARD-BUSINESS-LIFECYCLE-PASS')
        finally:
            child.stdin.close()
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
            child.stdout.close()
    with args.disk.open('rb') as disk:
        disk.seek(volume.start * 512 + 4096)
        if disk.read(512) != bytes((index + 31 * 17) & 255 for index in range(512)):
            raise RuntimeError('real board partition contents mismatch at guest LBA 8')
    print(f'Ordinary VM start/stop/restart and real partition write/read/flush passed: {args.log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    prep = commands.add_parser('prepare')
    for option in ('package', 'fit-pack', 'output'):
        prep.add_argument('--' + option, type=Path, required=True)
    smoke = commands.add_parser('run')
    smoke.add_argument('--qemu', required=True)
    for option in ('image', 'initramfs', 'disk', 'board', 'log'):
        smoke.add_argument('--' + option, type=Path, required=True)
    args = parser.parse_args()
    (prepare if args.action == 'prepare' else run)(args)


if __name__ == '__main__':
    main()
