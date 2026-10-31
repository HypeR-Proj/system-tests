#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Run serial TCG comparisons; preserve raw samples, commands and disk counters."""
import argparse
import csv
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/qemu'))
from hyper_test.session import Session


class Monitor:
    def __init__(self, path):
        self.connection = socket.socket(socket.AF_UNIX)
        self.connection.settimeout(10)
        self.connection.connect(str(path))
        self.stream = self.connection.makefile('rwb')
        json.loads(self.stream.readline())
        self.query('qmp_capabilities')

    def query(self, command):
        self.stream.write(json.dumps({'execute': command}).encode() + b'\n')
        self.stream.flush()
        while True:
            reply = json.loads(self.stream.readline())
            if 'error' in reply:
                raise RuntimeError(reply)
            if 'return' in reply:
                return reply['return']

    def close(self):
        self.stream.close()
        self.connection.close()


def verify_disk(disk, output):
    # The fixture has one GPT partition. mtools reads it without a host mount.
    with disk.open('rb') as stream:
        stream.seek(2 * 512 + 32)
        sector = struct.unpack('<Q', stream.read(8))[0]
    recovered = output / 'write-readback.bin'
    subprocess.run(['mcopy', '-i', f'{disk}@@{sector * 512}', '::write.bin', str(recovered)], check=True)
    expected = bytes((i * 13 + 7) % 256 for i in range(256)) * (16 * 1024 * 1024 // 256)
    if recovered.read_bytes() != expected:
        raise RuntimeError('persistent write readback failed')
    recovered.unlink()


def boot(args, system, number):
    folder = args.output / f'{number:02d}-{system}'
    folder.mkdir()
    disk = folder / 'disk.img'
    shutil.copyfile(args.fixture / 'base.img', disk)
    # Both copies are host-cache-warm. This is deliberately not an SSD test.
    with disk.open('rb') as stream:
        hashlib.file_digest(stream, 'sha256')
    qmp_path = folder / 'qmp.sock'
    # macOS Unix socket names are limited to 104 bytes.
    if len(str(qmp_path).encode()) >= 104:
        raise ValueError('output path too long for QMP Unix socket')
    kernel = (args.hyper_image if system == 'hyper' and args.hyper_image else
              args.fixture / ('hyper.img' if system == 'hyper' else 'linux.img'))
    initrd = args.fixture / ('bootstrap.cpio' if system == 'hyper' else 'linux.cpio.gz')
    bootargs = ('earlycon=pl011,mmio32,0x09000000' if system == 'hyper' else
                'console=ttyAMA0 earlycon=pl011,mmio32,0x09000000 rdinit=/perf-init loglevel=4')
    command = [args.qemu, '-machine', 'virt-11.1,virtualization=on,gic-version=3',
               '-accel', 'tcg,thread=multi', '-cpu', 'max', '-smp', '4', '-m', '1G',
               '-nodefaults', '-display', 'none', '-nic', 'none', '-no-reboot',
               '-kernel', str(kernel), '-initrd', str(initrd), '-append', bootargs,
               '-global', 'virtio-mmio.force-legacy=false',
               '-drive', f'if=none,id=physicaldisk,format=raw,file={str(disk).replace(",", ",,")},cache=writeback',
               '-device', 'virtio-scsi-device,id=physicalscsi,iommu_platform=on',
               '-device', 'scsi-hd,drive=physicaldisk,bus=physicalscsi.0,scsi-id=0,lun=0',
               '-serial', 'stdio', '-monitor', 'none', '-qmp', f'unix:{qmp_path},server=on,wait=off']
    (folder / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    print(f'Starting {number}: {system}', flush=True)
    samples = []
    with Session(command, folder / 'serial.log',
                 failures=(b'BENCH-FAIL', b'Kernel panic', b'HypeR: fatal', b'HypeR KERNEL PANIC')) as vm:
        ready = (rb'hyper-sh\$ '
                 if system == 'hyper' else rb'BENCH-READY\n')
        vm.await_text(ready, timeout=240)
        vm.pump(3)
        monitor = Monitor(qmp_path)
        try:
            before = monitor.query('query-blockstats')
            vm.send(b'/bin/perf-bench\n')
            vm.await_text(rb'BENCH-BEGIN\n', timeout=60)
            start = time.monotonic()
            while True:
                line = vm.await_text(rb'BENCH(?:,[a-z0-9_]+,[0-9]+,[0-9]+,[0-9]+|-PASS)\n',
                                     timeout=600, match_only=True).decode().strip()
                if line == 'BENCH-PASS':
                    break
                _, name, sample, count, elapsed = line.split(',')
                record = {'system': system, 'boot': number, 'name': name,
                          'sample': int(sample), 'count': int(count), 'nanoseconds': int(elapsed)}
                samples.append(record)
                print(line, flush=True)
            wall_seconds = time.monotonic() - start
            after = monitor.query('query-blockstats')
        finally:
            monitor.close()
    verify_disk(disk, folder)
    result = {'system': system, 'boot': number, 'wall_seconds': wall_seconds,
              'samples': samples, 'blockstats_before': before, 'blockstats_after': after,
              'persistent_readback': 'passed'}
    (folder / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    # Only per-run disposable copies are removed; base images and logs remain.
    disk.unlink()
    print(f'Passed {system}: {wall_seconds:.2f}s', flush=True)
    return result


def summarize(results):
    groups = {}
    for result in results:
        boot_groups = {}
        for sample in result['samples']:
            key = (sample['system'], sample['name'])
            value = sample['count'] / (sample['nanoseconds'] / 1e9)
            boot_groups.setdefault(key, []).append(value)
        for key, values in boot_groups.items():
            groups.setdefault(key, []).append(statistics.median(values))
    summary = []
    for (system, name), values in sorted(groups.items()):
        scale = 1 if 'random' in name else 1024 * 1024
        summary.append({'system': system, 'name': name, 'boots': len(values),
                        'aggregation': 'median of per-boot medians',
                        'unit': 'IOPS' if scale == 1 else 'MiB/s',
                        'median': statistics.median(values) / scale,
                        'min': min(values) / scale, 'max': max(values) / scale})
    return summary


def main():
    if any(os.environ.get(name, '').lower() in ('1', 'true') for name in ('CI', 'GITHUB_ACTIONS')):
        raise SystemExit('Performance measurements are local/manual only; do not run them in CI.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--qemu', default='qemu-system-aarch64')
    parser.add_argument('--hyper-image', type=Path,
                        help='test another kernel with the unchanged baseline workload and disk')
    parser.add_argument('--order', nargs='+', choices=['hyper', 'linux'],
                        default=['hyper', 'linux', 'linux', 'hyper', 'hyper', 'linux'])
    args = parser.parse_args()
    args.fixture = args.fixture.resolve()
    if args.hyper_image:
        args.hyper_image = args.hyper_image.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    metadata = {'qemu': subprocess.check_output([args.qemu, '--version'], text=True),
                'order': args.order, 'fixture': str(args.fixture),
                'host': subprocess.check_output(['uname', '-a'], text=True),
                'limits': ['TCG system emulation, not hardware DRAM/SSD performance',
                           'Host cache warm; first read means fresh guest caches only',
                           'Single thread, buffered FAT32 file I/O, QD1',
                           'HypeR I/O VM resources are included within 4 CPUs and 1 GiB',
                           'QEMU counters cover checks and verification as well as timed I/O']}
    kernel = args.hyper_image or args.fixture / 'hyper.img'
    metadata['hyper_image'] = str(kernel)
    metadata['hyper_image_sha256'] = hashlib.sha256(kernel.read_bytes()).hexdigest()
    if platform.system() == 'Darwin':
        metadata['mac'] = {
            'hardware': subprocess.check_output(['sysctl', 'hw.memsize', 'hw.logicalcpu',
                                                'hw.model', 'machdep.cpu.brand_string'], text=True),
            'os': subprocess.check_output(['sw_vers'], text=True),
            'power': subprocess.check_output(['pmset', '-g', 'batt'], text=True)}
    (args.output / 'environment.json').write_text(json.dumps(metadata, indent=2) + '\n')
    results = []
    for index, system in enumerate(args.order):
        results.append(boot(args, system, index))
        (args.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    summary = summarize(results)
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    with (args.output / 'samples.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=['system', 'boot', 'name', 'sample', 'count', 'nanoseconds'])
        writer.writeheader()
        for result in results:
            writer.writerows(result['samples'])
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
