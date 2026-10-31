#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
"""Real supervisor-loop isolation test. Requires broker-test features in both
hyper-io-runtime, hyper-vm-manager and hyper-vm-runtime, never enabled by ordinary builds.

Prepare the business ITB with verify-board-business.py prepare first. This fixture
adds two independent board volumes. Client A's real HELLO reply is withheld until the already
running B completes RESET/RELEASE and kernel-proven mapping retirement. This
excludes image-loading speed from the fairness assertion. Each runtime also delays
readiness by 65 seconds: loading must not consume the 60-second handshake budget.
Manager closes its real admission endpoint after admitting both sessions;
existing sessions remain usable.
"""
import argparse
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from board_config import Board


def prepare(args):
    board = json.loads((ROOT / 'boards/qemu.json').read_text())
    board['io-vm'].pop('network-device', None)
    board['io-vm'].pop('networks', None)
    board['files'].pop('vm/alpine.itb')
    board['files']['vm/business.itb'] = 'business'
    board['disk']['partitions'] = [
        part for part in board['disk']['partitions'] if 'content' in part
    ] + [{'label': name, 'size-mib': 8} for name in ('slow', 'fast')]
    board['virtual-machines'] = [
        {'name': name, 'image': 'vm/business.itb', 'autostart': False,
         'disk': {'source': f'PARTLABEL={name}'},
         'configuration': {'vcpus': 1,
                           'memory-bytes': 128 * 1024 * 1024,
                           'bootargs': 'console=ttyAMA0 earlycon rdinit=/init loglevel=4 hyper.role=business hyper.test=hold'}}
        for name in ('slow', 'fast')]
    args.board.parent.mkdir(parents=True, exist_ok=True)
    args.board.write_text(json.dumps(board, indent=2) + '\n')
    Board.load(args.board)


def run(args):
    board = Board.load(args.board)
    command = [sys.executable, '-B', str(ROOT / 'scripts/run-io-vm.py'),
               '--qemu', args.qemu, '--image', str(args.image),
               '--initramfs', str(args.initramfs), '--disk', str(args.disk),
               '--board', str(args.board)]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open('wb') as log, selectors.DefaultSelector() as selector:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 env={**os.environ, 'QEMU_MEMORY': args.memory})
        selector.register(child.stdout, selectors.EVENT_READ)
        pending, history = bytearray(), bytearray()

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
                        raise RuntimeError('console closed')
                    log.write(data); log.flush()
                    data = data.replace(b'\r', b'')
                    pending.extend(data); history.extend(data)
                    if any(marker in history for marker in (b'Kernel panic', b'HypeR KERNEL PANIC',
                            b'bootstrap failed', b'business disk: FAIL', b'quiescence failed')):
                        raise RuntimeError(f'guest failure: {bytes(history[-4096:])!r}')
            raise TimeoutError(f'waiting for {pattern!r}: {bytes(pending[-4096:])!r}')

        def send(data):
            child.stdin.write(data); child.stdin.flush()

        command_number = 0

        def shell(line):
            nonlocal command_number
            command_number += 1
            marker = f'BROKER-COMMAND-{command_number}'.encode()
            # Terminal input remains in the original queue until a child reads.
            # vmm never reads stdin, so this result fence survives foreground
            # execution. Input echo is not a boundary: guest/kernel output may
            # interleave in the middle of the echoed command.
            # Quoted concatenation keeps the complete result marker out of
            # input echo; unrelated logs may prefix the actual output line.
            quoted = marker.replace(b'-COMMAND-', b"-''COMMAND-", 1)
            send(line.encode() + b'\necho ' + quoted + b'\n')
            output = await_text(re.escape(marker) + rb'\n')
            await_text(rb'hyper-sh\$ ')
            if b'vmm:' in output or b'sh: command' in output:
                raise RuntimeError(f'command failed: {output!r}')
            return output

        try:
            await_text(rb'\[HypeR bootstrap stage 1\]\[io-runtime\] ready; bootstrap mounts complete\n')
            await_text(rb'hyper-sh\$ ')
            # Init's initial provisioning can complete after storage READY.
            limit = time.monotonic() + 60
            while True:
                listing = shell('vmm list')
                if all(re.search(name + rb'\s+stopped\b', listing) for name in (b'slow', b'fast')):
                    break
                if time.monotonic() >= limit:
                    raise TimeoutError('board definitions not provisioned')
                time.sleep(0.1)
            def verify_guest(name):
                # Includes the deliberate 65-second pre-admission delay plus
                # image loading. The actual protocol deadline remains 60 seconds.
                limit = time.monotonic() + 180
                while True:
                    status = shell(f'vmm status {name}')
                    if re.search(name.encode() + rb'\s+running\b', status):
                        break
                    if re.search(name.encode() + rb'\s+(failed|stopped)\b', status):
                        raise RuntimeError(f'{name} failed before becoming runnable: {status!r}')
                    if time.monotonic() >= limit:
                        raise TimeoutError(f'{name} did not reach running')
                    time.sleep(0.1)
                send(f'vmm console {name}\n'.encode())
                await_text(rb'HypeR business disk: acceptance complete\n')
                send(b'\x1dd'); await_text(rb'hyper-sh\$ ')

            # Exercise repeated listener handoffs before guest loading. A stale
            # READABLE observation must not turn a nonblocking accept into a
            # fatal supervisor error.
            for _ in range(600):
                shell('vmm list')
            shell('vmm start fast')
            verify_guest('fast')
            shell('vmm start slow')
            if b'BROKER-TEST REPLY-HELD client=1 generation=1' not in history:
                await_text(rb'BROKER-TEST REPLY-HELD client=1 generation=1\n')
            shell('vmm stop fast')
            verify_guest('slow')
            # /data uses the same backend but no admission endpoint.
            shell('echo broker-alive > /data/broker-isolation.txt')
            output = shell('cat /data/broker-isolation.txt')
            if b'broker-alive' not in output:
                raise RuntimeError('configuration volume stopped after manager endpoint death')
            required = (b'BROKER-TEST BOUND client=2 generation=1',
                        b'BROKER-TEST HELLO-SENT client=1 generation=1',
                        b'BROKER-TEST REPLY-HELD client=1 generation=1',
                        b'BROKER-TEST RELEASED client=2 generation=1',
                        b'BROKER-TEST BOUND client=1 generation=1')
            positions = [history.find(marker) for marker in required]
            if any(position < 0 for position in positions) or positions != sorted(positions):
                raise RuntimeError(f'client progress ordering mismatch: {positions}')
            for marker in (b'BROKER-TEST MANAGER-ENDPOINT-CLOSED', b'new client admissions disabled:'):
                if marker not in history:
                    raise RuntimeError(f'missing actual admission closure observation: {marker!r}')
            shell('vmm stop slow')
            limit = time.monotonic() + 60
            while True:
                listing = shell('vmm list')
                if all(re.search(name + rb'\s+stopped\b', listing) for name in (b'slow', b'fast')):
                    break
                if time.monotonic() >= limit:
                    raise TimeoutError('disk sessions failed to stop after admission endpoint loss')
                time.sleep(0.1)
            shell('echo BOARD-BROKER-ISOLATION-PASS')
        except BaseException:
            # The panic marker precedes the register dump and backtrace. Keep
            # collecting raw evidence briefly before terminating QEMU, as the
            # shared Session transport does, preserving the original failure.
            deadline = time.monotonic() + 0.5
            try:
                while (remaining := deadline - time.monotonic()) > 0:
                    events = selector.select(min(0.05, remaining))
                    if not events and child.poll() is not None:
                        break
                    for key, _ in events:
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            break
                        log.write(data); log.flush()
            except OSError:
                pass  # Preserve the test failure if the transport has closed.
            raise
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
        for volume in board.partitions:
            if volume.name in ('slow', 'fast'):
                disk.seek(volume.start * 512 + 4096)
                if disk.read(512) != bytes((index + 31 * 17) & 255 for index in range(512)):
                    raise RuntimeError(f'{volume.name}: real partition contents mismatch')
    print(f'Broker fair progress, endpoint loss, active disks and /data passed: {args.log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--board', type=Path, required=True)
    smoke = commands.add_parser('run')
    smoke.add_argument('--qemu', required=True)
    smoke.add_argument('--memory', default='1G', help='RAM for I/O VM plus two 128 MiB guests')
    for name in ('image', 'initramfs', 'disk', 'board', 'log'):
        smoke.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    (prepare if args.action == 'prepare' else run)(args)


if __name__ == '__main__':
    main()
