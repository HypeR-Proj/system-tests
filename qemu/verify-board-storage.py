#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Exercise the Native FAT volume and verify its contents after a cold VM restart."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from board_config import Board
from hyper_test.shell_status import checked, status


def boot(args, mode):
    launcher = Path(__file__).resolve().parents[2] / 'scripts/run-io-vm.py'
    command = [sys.executable, '-B', str(launcher), '--qemu', args.qemu,
               '--image', str(args.image), '--initramfs', str(args.initramfs),
               '--disk', str(args.disk), '--board', str(args.board)]
    with args.log.with_suffix(f'.{mode}.log').open('wb') as log, selectors.DefaultSelector() as selector:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        pending = bytearray()
        selector.register(child.stdout, selectors.EVENT_READ)

        def await_text(pattern, timeout=180):
            limit = time.monotonic() + timeout
            while time.monotonic() < limit:
                match = re.search(pattern, pending)
                if match:
                    result = bytes(pending[:match.end()])
                    del pending[:match.end()]
                    return result
                if child.poll() is not None:
                    raise RuntimeError(f'QEMU exited with {child.returncode}')
                for key, _ in selector.select(0.1):
                    data = os.read(key.fd, 65536)
                    if not data:
                        raise RuntimeError('QEMU closed its console')
                    log.write(data)
                    log.flush()
                    pending.extend(data.replace(b'\r', b''))
                    if any(marker in pending for marker in (
                            b'Kernel panic', b'HypeR: fatal', b'HypeR KERNEL PANIC',
                            b'bootstrap failed', b'BOARD-STORAGE: ' + mode.encode() + b' FAIL',
                            b'[HypeR bootstrap stage 1][io-runtime] failed:', b'HypeR io-runtime: failed:')):
                        # Preserve the complete crash report before terminating
                        # QEMU; its banner often arrives in a separate read.
                        drain_until = time.monotonic() + 3
                        while time.monotonic() < drain_until:
                            for diagnostic, _ in selector.select(0.1):
                                tail = os.read(diagnostic.fd, 65536)
                                if tail:
                                    log.write(tail)
                                    log.flush()
                                    pending.extend(tail.replace(b'\r', b''))
                        raise RuntimeError(f'boot/storage failure: {bytes(pending[-4096:])!r}')
            raise TimeoutError(f'waiting for {pattern!r}: {bytes(pending[-4096:])!r}')

        try:
            if args.require_userspace_device:
                await_text(rb'\[HypeR bootstrap stage 1\]\[io-runtime\] device permission attenuation passed\n')
                await_text(rb'DEVICE-TEST: worker prepared\n')
            boot = await_text(rb'\[HypeR bootstrap stage 1\]\[io-runtime\] ready; bootstrap mounts complete\n')
            if b'[HypeR bootstrap stage 1][io-runtime] IO VM: ' not in boot:
                raise RuntimeError('forwarded I/O VM log prefix missing')
            await_text(rb'hyper-sh\$ ')
            child.stdin.write(f'/bin/storage-probe {mode}\n'.encode())
            child.stdin.flush()
            if mode == 'write':
                await_text(rb'BOARD-STORAGE: remote recursive removal PASS\n', timeout=180)
            await_text(b'BOARD-STORAGE: ' + mode.encode() + b' PASS\n', timeout=600)
            await_text(rb'hyper-sh\$ ')
            if args.require_block_queue:
                # The provider reports through its supervisor's journal stream.
                # Query the durable record instead of depending on boot serial
                # interleaving or whether a splash renderer consumed the line.
                child.stdin.write(checked('journalctl -n 100'))
                child.stdin.flush()
                journal = await_text(rb'hyper-sh\$ ')
                if status(journal) != 0 or b'BLOCK-QUEUE: PASS ' not in journal:
                    raise RuntimeError(f'Native block queue probe missing: {journal!r}')
            child.stdin.write(checked('cp /etc/hyper/components.json /data/copied-components.json'))
            child.stdin.flush()
            copied = await_text(rb'hyper-sh\$ ')
            if status(copied) != 0:
                raise RuntimeError(f'copy across system/FAT failed: {copied!r}')
            child.stdin.write(f'vmm status {args.observed_name}\n'.encode())
            child.stdin.flush()
            observed = await_text(rb'hyper-sh\$ ')
            if not re.search(re.escape(args.observed_name.encode())
                             + rb'\s+running\s+yes\s+read-only\s+'
                             + re.escape(args.observed_image.encode()) + rb'\n', observed):
                raise RuntimeError(f'I/O VM observation unavailable: {observed!r}')
            if b'vCPUs:' not in observed or b'RAM capacity: 64 MiB' not in observed:
                raise RuntimeError(f'I/O VM metrics missing: {observed!r}')
            for affinity in args.io_configuration.get('affinity', []):
                vcpu = affinity['vcpu']
                placement = re.search(fr'vCPU {vcpu}: pCPU ([0-9]+)\b'.encode(), observed)
                if placement is None or int(placement[1]) not in affinity['cpus']:
                    raise RuntimeError(f'configured I/O VM affinity not applied: {observed!r}')
            # Account for the GPT owner pool and one pool per Native volume.
            resident = re.search(rb'allocated VM backing: ([0-9]+) bytes', observed)
            if resident is None or not 0 < int(resident[1]) <= args.maximum_io_backing:
                raise RuntimeError(
                    f'I/O VM allocated backing missing or exceeds {args.maximum_io_backing} bytes: '
                    f'{observed!r}')
            # The real appliance agent appears only after system takeover.
            # Wait for two samples, independently of storage-test duration.
            sample_deadline = time.monotonic() + 45
            while b'guest CPU:' not in observed or b'% busy;' not in observed:
                if time.monotonic() >= sample_deadline:
                    raise RuntimeError(f'I/O VM guest agent did not become ready: {observed!r}')
                time.sleep(1)
                child.stdin.write(f'vmm status {args.observed_name}\n'.encode())
                child.stdin.flush()
                observed = await_text(rb'hyper-sh\$ ')
            cpu = re.search(rb'guest CPU: ([0-9]+\.[0-9]+)% busy; ([0-9]+\.[0-9]+)% I/O wait', observed)
            memory = re.search(rb'guest memory: ([0-9]+) KiB used / ([0-9]+) KiB total; ([0-9]+) KiB available; ([0-9]+) KiB free', observed)
            if cpu is None or not 0 <= sum(map(float, cpu.groups())) <= 100:
                raise RuntimeError(f'invalid guest CPU sample: {observed!r}')
            if memory is None:
                raise RuntimeError(f'missing guest memory sample: {observed!r}')
            used, total, available, free = map(int, memory.groups())
            if not 0 < total <= 64 * 1024 or used + available != total or not 0 <= free <= total:
                raise RuntimeError(f'invalid guest memory sample: {observed!r}')
            # Observation is granted by the broker; no management capability is
            # held for this target, including console and placement operations.
            commands = [f'vmm {action} {args.observed_name}' for action in
                        ('start', 'stop', 'restart', 'delete', 'console')]
            commands.append(f'vmm affinity {args.observed_name} 0 0')
            for command in commands:
                child.stdin.write(checked(command))
                child.stdin.flush()
                refused = await_text(rb'hyper-sh\$ ')
                if (f"VM '{args.observed_name}' is read-only: vm-manager has no management capability".encode() not in refused
                        or status(refused) == 0):
                    raise RuntimeError(f'{command}: missing capability rejection: {refused!r}')
            child.stdin.write(f'vmm status {args.observed_name}\n'.encode())
            child.stdin.flush()
            if not re.search(re.escape(args.observed_name.encode()) + rb'\s+running\s+yes\s+read-only',
                             await_text(rb'hyper-sh\$ ')):
                raise RuntimeError('rejected control request changed the observed VM')
            # This fixture may intentionally have no business VMs. Submit a
            # complete definition so the request reaches manager admission.
            child.stdin.write(args.conflict_config_command)
            child.stdin.flush()
            prepared = await_text(rb'hyper-sh\$ ')
            if b'sh: ' in prepared or b'echo:' in prepared:
                raise RuntimeError(f'cannot prepare name-conflict configuration: {prepared!r}')
            child.stdin.write(
                checked(f'vmm create {args.observed_name} --config /data/observed-name-conflict.json'))
            child.stdin.flush()
            refused = await_text(rb'hyper-sh\$ ')
            if (f"VM '{args.observed_name}' already exists (observed through broker)".encode() not in refused
                    or status(refused) == 0):
                raise RuntimeError(f'observed VM name was not protected from shadowing: {refused!r}')
            # Keep the real human-paced console path in coverage too.
            for character in b'echo BOARD-SHELL-RESPONSIVE\n':
                child.stdin.write(bytes([character]))
                child.stdin.flush()
                time.sleep(0.08)
            await_text(rb'\nBOARD-SHELL-RESPONSIVE\n')
        finally:
            child.stdin.close()
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            child.stdout.close()


def audit_stacks(args):
    spec = importlib.util.spec_from_file_location(
        'stack_acceptance', Path(__file__).with_name('verify-stack.py'))
    stack = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stack)
    try:
        for mode in (('verify',) if args.verify_only else ('write', 'verify')):
            stack.audit_summary(args.log.with_suffix(f'.{mode}.log').read_bytes(),
                                args.minimum_stack_remaining or 0,
                                required_kinds=frozenset({'kernel', 'user', 'irq'}),
                                maximum_used=args.maximum_stack_used)
    except ValueError:
        if args.stack_elf is not None:
            # The acceptance VMs have exited. Diagnose a disposable snapshot
            # with the matching ELF; no diagnostic outcome changes the failure.
            command = [sys.executable, '-B', str(Path(__file__).with_name('watch-stack.py')),
                       '--qemu', args.qemu, '--image', str(args.image),
                       '--elf', str(args.stack_elf), '--config', str(args.stack_config),
                       '--fixture', str(args.disk.parent),
                       '--output', str(args.log.parent / 'stack-watch'),
                       '--timeout', '90', '--minimum-remaining', '4096',
                       '--command', '/bin/storage-probe verify',
                       '--command', 'cp /etc/hyper/components.json /data/copied-components.json',
                       '--command', f'vmm status {args.observed_name}',
                       '--command', f'vmm affinity {args.observed_name} 0 0']
            try:
                subprocess.run(command, check=False)
            except OSError as error:
                print(f'Stack diagnostic unavailable: {error}', file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--minimum-stack-remaining', type=int)
    parser.add_argument('--maximum-stack-used', type=int)
    parser.add_argument('--stack-elf', type=Path, help='matching ELF for bounded failure diagnosis')
    parser.add_argument('--stack-config', type=Path, help='configuration used to build the matching ELF')
    parser.add_argument('--require-userspace-device', action='store_true',
                        help='require the generic userspace MMIO/IRQ worker')
    parser.add_argument('--require-block-queue', action='store_true',
                        help='require bounded async queue acceptance from the FAT provider')
    parser.add_argument('--verify-only', action='store_true',
                        help='inspect an existing acceptance disk without rewriting its payload')
    for name in ('image', 'initramfs', 'disk', 'board', 'log'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if (args.stack_elf is None) != (args.stack_config is None):
        parser.error('--stack-elf and --stack-config must be supplied together')
    board = Board.load(args.board)
    io_vm = board.source['io-vm']
    args.observed_name = io_vm['name']
    args.observed_image = io_vm['image']
    args.io_configuration = io_vm['configuration']
    # NativeBlock reserves a 1 MiB shared-memory pool for each client.
    args.maximum_io_backing = (args.io_configuration['memory-bytes']
                               + (1 + len(board.native_volumes())) * 1024 * 1024)
    conflict_config = {'format': 'hyper.vm-config', 'virtual-machines': [
        {field: io_vm[field] for field in ('name', 'image', 'configuration')}]}
    command = ('echo ' + shlex.quote(json.dumps(conflict_config, separators=(',', ':')))
               + ' > /data/observed-name-conflict.json')
    if len(command.encode()) > 512:
        parser.error('name-conflict fixture exceeds the Native shell command-line limit')
    args.conflict_config_command = command.encode() + b'\n'
    if args.minimum_stack_remaining is not None and args.minimum_stack_remaining < 0:
        parser.error('stack reserve must be nonnegative')
    if args.maximum_stack_used is not None and args.maximum_stack_used < 1:
        parser.error('maximum stack usage must be positive')
    args.log.parent.mkdir(parents=True, exist_ok=True)
    if not args.verify_only:
        boot(args, 'write')
    boot(args, 'verify')
    if args.minimum_stack_remaining is not None or args.maximum_stack_used is not None:
        audit_stacks(args)
    checks = ('existing Native volume readback and interactive shell' if args.verify_only
              else 'Native board storage, cold restart and interactive shell')
    print(f'{checks} passed: {args.log.parent}')


if __name__ == '__main__':
    main()
