#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Manually measure cold and repeated Alpine starts; never impose CI timing gates."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/qemu'))
from hyper_test.session import Session
sys.path.insert(0, str(ROOT / 'scripts'))
from board_config import Board

_runner_spec = importlib.util.spec_from_file_location('board_runner', ROOT / 'scripts/run-io-vm.py')
_runner = importlib.util.module_from_spec(_runner_spec)
_runner_spec.loader.exec_module(_runner)


def shell(vm, command, marker):
    # Split the marker in the echoed input so only executed output can match.
    quoted = marker.replace('-COMMAND-', "-''COMMAND-", 1)
    vm.send(f'{command}\necho {quoted}\n'.encode())
    output = vm.await_text(re.escape(marker.encode()) + rb'\n', timeout=60)
    vm.await_text(rb'hyper-sh\$ ', timeout=30)
    if b'vmm:' in output or b'sh: command' in output:
        raise RuntimeError(f'command failed: {output!r}')
    return output


def stop(vm, index):
    shell(vm, 'vmm stop alpine', f'START-COMMAND-{index}-STOP')
    deadline = time.monotonic() + 60
    attempt = 0
    while True:
        output = shell(vm, 'vmm status alpine', f'START-COMMAND-{index}-STATUS-{attempt}')
        if re.search(rb'\balpine\s+stopped\b', output):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f'Alpine did not stop: {output!r}')
        vm.pump(0.1)
        attempt += 1


def start_result(vm, started, index):
    output = vm.await_text(rb'HypeR vm-runtime: vCPU start submitted in ([0-9]+\.[0-9]+) ms \(from main\)\n', timeout=240)
    submitted = time.monotonic() - started
    duration = re.search(rb'vCPU start submitted in ([0-9]+\.[0-9]+) ms', output)
    vm.send(b'vmm console alpine\n')
    vm.await_text(rb'HypeR guest: Linux userspace is running\n', timeout=180)
    vm.await_text(rb'~ # ', timeout=60)
    ready = time.monotonic() - started
    vm.send(f"echo LOCAL-''BOOT-PASS-{index}\n".encode())
    vm.await_text(f'\nLOCAL-BOOT-PASS-{index}\n'.encode(), timeout=30)
    vm.await_text(rb'~ # ', timeout=30)
    vm.send(b'\x1dd')
    vm.await_text(rb'hyper-sh\$ ', timeout=30)
    payloads = re.findall(rb'payload ([0-9]+) bytes read=([0-9]+) us write=([0-9]+) us', output)
    return {'start': index, 'start_to_vcpu_seconds': submitted,
            'runtime_main_to_vcpu_ms': float(duration[1]),
            'start_to_guest_shell_seconds': ready, 'guest_shell': 'passed',
            'profile_lines': [line.decode() for line in output.splitlines() if b'startup profile:' in line],
            'payloads': [{'bytes': int(size), 'read_us': int(read), 'write_us': int(write)}
                         for size, read, write in payloads]}


def boot(args, index):
    folder = args.output / str(index)
    folder.mkdir()
    # This warms only the host file cache, not either guest's filesystem cache.
    with (args.fixture / 'base.img').open('rb') as stream:
        disk_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
    machine = _runner.machine_arguments(args.board, 'virt-11.1,virtualization=on,gic-version=3')
    command = [args.qemu, '-machine', machine,
               '-accel', 'tcg,thread=multi', '-cpu', 'max', '-smp', '4', '-m', '1G',
               '-nodefaults', '-display', 'none', '-nic', 'none', '-no-reboot',
               '-kernel', str(args.image), '-initrd', str(args.initramfs),
               '-append', 'earlycon=pl011,mmio32,0x09000000',
               '-global', 'virtio-mmio.force-legacy=false', '-snapshot',
               '-drive', f'if=none,id=disk,format=raw,file={str(args.fixture / "base.img").replace(",", ",,")},cache=writeback',
               '-device', _runner.storage_controller(args.board),
               '-device', 'scsi-hd,drive=disk,bus=physicalscsi.0,scsi-id=0,lun=0',
               '-serial', 'stdio', '-monitor', 'none']
    command.extend(_runner.network_arguments(args.board))
    (folder / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    start = time.monotonic()
    with Session(command, folder / 'serial.log', failures=(b'HypeR: fatal', b'HypeR KERNEL PANIC',
                 b'Kernel panic', b'HypeR vm-runtime: failed',
                 b'[HypeR bootstrap stage 1][io-runtime] failed', b'HypeR io-runtime: failed')) as vm:
        starts = [start_result(vm, start, 0)]
        for restart in range(1, args.starts):
            stop(vm, restart)
            started = time.monotonic()
            vm.send(b'vmm start alpine\n')
            starts.append(start_result(vm, started, restart))
        if args.starts > 1:
            stop(vm, args.starts)
    cold = starts[0]
    result = {'boot': index, 'disk_sha256': disk_sha256,
              'cold_start_to_vcpu_seconds': cold['start_to_vcpu_seconds'],
              'runtime_main_to_vcpu_ms': cold['runtime_main_to_vcpu_ms'],
              'cold_start_to_guest_shell_seconds': cold['start_to_guest_shell_seconds'],
              'guest_shell': 'passed',
              'profile_lines': cold['profile_lines'], 'starts': starts}
    (folder / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)
    return result


def main():
    if any(os.environ.get(name, '').lower() in ('1', 'true') for name in ('CI', 'GITHUB_ACTIONS')):
        raise SystemExit('Performance measurements are local/manual only; do not run them in CI.')
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('fixture', 'image', 'output'):
        parser.add_argument('--' + option, type=Path, required=True)
    parser.add_argument('--initramfs', type=Path)
    parser.add_argument('--qemu', default='qemu-system-aarch64')
    parser.add_argument('--boots', type=int, default=3)
    parser.add_argument('--starts', type=int, default=1,
                        help='Starts per boot; subsequent starts alternate vmm stop/start alpine')
    args = parser.parse_args()
    if args.boots < 1:
        parser.error('--boots must be positive')
    if args.starts < 1:
        parser.error('--starts must be positive')
    args.fixture = args.fixture.resolve()
    args.board = Board.load(args.fixture / 'board.json')
    args.image = args.image.resolve()
    args.initramfs = (args.initramfs or args.fixture / 'bootstrap.cpio').resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    metadata = {'qemu': subprocess.check_output([args.qemu, '--version'], text=True),
                'host': subprocess.check_output(['uname', '-a'], text=True),
                'sha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in (args.image, args.initramfs, args.fixture / 'board.json')},
                'limits': ['Fresh guests, warm host disk cache, snapshot writes discarded',
                           'Later starts reuse the host-kernel cache within the same boot',
                           'The manager retains its image handle between start/stop commands',
                           'Guest-shell interval includes console attachment',
                           'Runtime metric ends at start submission, not first guest entry',
                           'Profile-enabled runs are diagnostic, not final performance results']}
    (args.output / 'environment.json').write_text(json.dumps(metadata, indent=2) + '\n')
    results = [boot(args, index) for index in range(args.boots)]
    (args.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    keys = ('cold_start_to_vcpu_seconds', 'runtime_main_to_vcpu_ms', 'cold_start_to_guest_shell_seconds')
    summary = {key: {'median': statistics.median(r[key] for r in results),
                     'min': min(r[key] for r in results), 'max': max(r[key] for r in results)} for key in keys}
    for ordinal in range(1, args.starts):
        summary[f'restart_{ordinal}'] = {
            key: {'median': statistics.median(r['starts'][ordinal][key] for r in results),
                  'min': min(r['starts'][ordinal][key] for r in results),
                  'max': max(r['starts'][ordinal][key] for r in results)}
            for key in ('runtime_main_to_vcpu_ms', 'start_to_vcpu_seconds', 'start_to_guest_shell_seconds')}
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
