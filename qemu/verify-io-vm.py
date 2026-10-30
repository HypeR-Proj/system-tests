#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Package and verify cross-VM virtio-scsi against a disposable QEMU disk."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import time

from hyper_test.session import native_command

MIB = 1024 * 1024
DISK_BYTES = 8 * MIB
WRITE_OFFSET = 8 * 512
FINAL_PATTERN = bytes((index + 31 * 17) & 255 for index in range(512))
HOST_LOG = re.compile(rb'<[0-7]>\[[ \t]*[0-9]+\.[0-9]+\] [^\n]*\n')
FAILURE_MARKERS = (b'IO-VM-SMOKE: FAIL', b'HypeR: fatal', b'kernel panic',
                   b'Kernel panic', b'HypeR KERNEL PANIC', b'HypeR crash monitor',
                   b'kernel startup failed', b'HypeR business disk: FAIL')


_images_spec = importlib.util.spec_from_file_location(
    'hyper_io_images', Path(__file__).resolve().parents[2] / 'scripts/io-vm-images.py')
_images = importlib.util.module_from_spec(_images_spec)
_images_spec.loader.exec_module(_images)
package_payloads = _images.package_payloads
prepare = _images.prepare


def verify_disk(path):
    """Check the guest's final round and reject writes outside the test extent."""
    if path.stat().st_size != DISK_BYTES:
        raise ValueError('unexpected physical disk size')
    with path.open('rb') as stream:
        if any(stream.read(WRITE_OFFSET)):
            raise ValueError('disk bytes before LBA 8 changed')
        if stream.read(len(FINAL_PATTERN)) != FINAL_PATTERN:
            raise ValueError('physical disk does not contain the final business-guest write')
        while chunk := stream.read(MIB):
            if any(chunk):
                raise ValueError('disk bytes after the test extent changed')


def append_output(pending, data):
    pending.extend(data.replace(b'\r', b''))
    if any(marker in pending for marker in FAILURE_MARKERS):
        raise RuntimeError('cross-VM fixture reported a failure')
    pending[:] = HOST_LOG.sub(b'', pending)
    if any(marker in pending for marker in FAILURE_MARKERS):
        raise RuntimeError('cross-VM fixture reported a failure')
    passed = b'IO-VM-SMOKE: PASS\n' in pending
    if len(pending) > 65536:
        del pending[:-65536]
    return passed


def run(qemu, image, initramfs, logfile, timeout, test="basic"):
    logfile.parent.mkdir(parents=True, exist_ok=True)
    # Keep the uniquely named disk as evidence. Never reuse an old disk whose
    # final pattern could let a run with no actual I/O falsely pass.
    evidence = Path(tempfile.mkdtemp(prefix=logfile.stem + '-', dir=logfile.parent))
    disk = evidence / 'physical-disk.img'
    with disk.open('xb') as stream:
        stream.truncate(DISK_BYTES)
    command = native_command(qemu, image, initramfs) + [
        '-nic', 'none', '-global', 'virtio-mmio.force-legacy=false',
        '-drive', 'if=none,id=physicaldisk,format=raw,file='
        + str(disk).replace(',', ',,') + ',cache=writeback',
        '-device', 'virtio-scsi-device,id=physicalscsi,iommu_platform=on',
        '-device', 'scsi-hd,drive=physicaldisk,bus=physicalscsi.0,scsi-id=0,lun=0']
    started = time.monotonic()
    pending = bytearray()
    with logfile.open('wb') as log, selectors.DefaultSelector() as selector:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = started + timeout
            while time.monotonic() < deadline:
                for key, _ in selector.select(0.2):
                    data = os.read(key.fd, 65536)
                    if not data:
                        raise RuntimeError('QEMU closed output before fixture completion')
                    log.write(data)
                    log.flush()
                    if append_output(pending, data):
                        break
                else:
                    if process.poll() is not None:
                        raise RuntimeError(f'QEMU exited before completion: {process.returncode}')
                    continue
                break
            else:
                raise TimeoutError(f'cross-VM fixture exceeded {timeout:g} seconds')
        except Exception:
            # A failure prefix can arrive before the formatted reason. Retain
            # a bounded tail before terminating our QEMU, without changing the
            # failed result or waiting for a potentially broken guest to exit.
            drain_deadline = time.monotonic() + 0.5
            while time.monotonic() < drain_deadline:
                events = selector.select(max(0, drain_deadline - time.monotonic()))
                if not events:
                    break
                data = os.read(events[0][0].fd, 65536)
                if not data:
                    break
                log.write(data)
                log.flush()
                pending.extend(data.replace(b'\r', b''))
                if len(pending) > 65536:
                    del pending[:-65536]
            sys.stderr.write(pending[-16384:].decode(errors='replace'))
            sys.stderr.write(f'\nRetained physical disk: {disk}\n')
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
    if test == 'reset' and b'HypeR business disk: reset/rebind PASS' not in pending:
        raise RuntimeError('appliance did not confirm reset/rebind acceptance')
    verify_disk(disk)
    result = {'passed': True, 'disk': str(disk), 'log': str(logfile),
              'test': test, 'rounds': 64 if test == 'reset' else 32, 'offset': WRITE_OFFSET, 'bytes': len(FINAL_PATTERN),
              'elapsed_seconds': round(time.monotonic() - started, 3),
              'pattern_sha256': hashlib.sha256(FINAL_PATTERN).hexdigest()}
    (evidence / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f'Cross-VM physical-disk read/write/flush acceptance passed: {logfile}')
    print(f'Host disk verification and evidence: {evidence}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    packing = commands.add_parser('prepare')
    packing.add_argument('--package', type=Path, required=True)
    packing.add_argument('--fit-pack', type=Path, required=True)
    packing.add_argument('--output', type=Path, required=True)
    packing.add_argument('--test', choices=('basic', 'reset'), default='basic')
    running = commands.add_parser('run')
    running.add_argument('--qemu', required=True)
    running.add_argument('--test', choices=('basic', 'reset'), default='basic')
    running.add_argument('--image', type=Path, required=True)
    running.add_argument('--initramfs', type=Path, required=True)
    running.add_argument('--log', type=Path, required=True)
    running.add_argument('--timeout', type=float,
                         default=float(os.environ.get('QEMU_IO_VM_TIMEOUT_SECONDS', '600')))
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args.package, args.fit_pack, args.output, args.test)
    else:
        if not 0 < args.timeout <= 3600:
            parser.error('timeout must be between 0 and 3600 seconds')
        run(args.qemu, args.image, args.initramfs, args.log, args.timeout, args.test)


if __name__ == '__main__':
    main()
