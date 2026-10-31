#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Qualify post-adoption PCIe console discovery, binary I/O and driver rebind.

The explicit test appliance contains a Linux probe; the separately built Native
io-runtime enables its bounded echo consumer with the console-test feature.
Neither fixture nor echo mode enters ordinary images. There is no sysfs PCI
rescan: the running Linux guest must discover the endpoint through pciehp.
"""

import argparse
import copy
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import sys
import time

from hyper_test.guest_console import append_console_output
from hyper_test.session import Session
from hyper_test.shell_status import checked, status

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from board_config import Board

PREFIX = b'HypeR console hotplug: '
MARKERS = (
    PREFIX + b'bootstrap slot empty',
    b'HypeR io-runtime: virtio-console inserted on PCIe slot 1',
    b'HypeR io-runtime: running backend adopted',
    PREFIX + b'named port automatically discovered',
    PREFIX + b'binary round-trip 1 PASS',
    PREFIX + b'binary round-trip 2 PASS',
    PREFIX + b'acceptance PASS',
)
FAILURES = (
    b'HypeR: fatal', b'Kernel panic', b'HypeR KERNEL PANIC',
    b'bootstrap failed', b'provider adoption failed', b'queue rejected',
    PREFIX + b'FAIL:', PREFIX + b'endpoint present before takeover',
)
MOUNT = b'mount -t devtmpfs devtmpfs /dev\n'
START_PROBE = b'''mkdir -p /run
/console-hotplug &
probe_pid=$!
attempt=0
while [ ! -f /run/console-hotplug.ready ]; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 20 ] || [ ! -d "/proc/$probe_pid" ]; then
        echo 'HypeR console hotplug: FAIL: probe startup'
        exit 1
    fi
    sleep 1
done
'''


def init_script(archive):
    """Read the regular init without extracting paths or rewriting hardlinks."""
    offset = 0
    result = None
    while offset < len(archive):
        if archive[offset] == 0:
            offset += 1
            continue
        header = archive[offset:offset + 110]
        if len(header) != 110 or header[:6] != b'070701':
            raise ValueError('invalid appliance newc header')
        fields = [int(header[6 + index * 8:14 + index * 8], 16) for index in range(13)]
        namesize, size = fields[11], fields[6]
        name_end = offset + 110 + namesize
        start = (name_end + 3) & ~3
        end = start + size
        if namesize < 1 or name_end > len(archive) or end > len(archive):
            raise ValueError('truncated appliance newc record')
        name = archive[offset + 110:name_end]
        if name[-1:] != b'\0':
            raise ValueError('unterminated appliance newc name')
        if name[:-1] in (b'init', b'./init'):
            if fields[1] & 0o170000 != 0o100000 or not size:
                raise ValueError('test appliance requires a regular, nonempty init')
            result = archive[start:end]
        offset = (end + 3) & ~3
    if result is None or not result.startswith(b'#!/bin/sh\n') or result.count(MOUNT) != 1:
        raise ValueError('unsupported appliance init; expected one devtmpfs mount')
    # The probe inspects sysfs and must start before any storage backend can
    # let Native init reach the successor environment.
    before = result.split(MOUNT, 1)[0]
    if b'mount -t sysfs sysfs /sys\n' not in before:
        raise ValueError('appliance init must mount sysfs before devtmpfs')
    return result.replace(MOUNT, MOUNT + START_PROBE, 1)


def overlay(files):
    """Executable-only newc overlay; preserve the upstream archive verbatim."""
    output = bytearray()
    entries = [(name, 0o100755, data) for name, data in files.items()]
    entries.append(('TRAILER!!!', 0, b''))
    for inode, (name, mode, data) in enumerate(entries, 1):
        encoded = name.encode() + b'\0'
        output.extend(b'\0' * (-len(output) % 4))
        fields = (inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0)
        output.extend(b'070701' + ''.join(f'{field:08x}' for field in fields).encode())
        output.extend(encoded)
        output.extend(b'\0' * (-len(output) % 4))
        output.extend(data)
    output.extend(b'\0' * (-len(output) % 4))
    return bytes(output)


def static_probe(path):
    data = path.read_bytes()
    if (len(data) < 64 or len(data) > 4 * 1024 * 1024
            or data[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<H', data, 18)[0] != 183):
        raise ValueError('probe must be a bounded AArch64 ELF executable')
    offset = struct.unpack_from('<Q', data, 32)[0]
    entry, count = struct.unpack_from('<HH', data, 54)
    if entry != 56 or not count or offset + entry * count > len(data):
        raise ValueError('invalid probe program headers')
    if any(struct.unpack_from('<I', data, offset + index * entry)[0] == 3
           for index in range(count)):
        raise ValueError('probe must be statically linked; no Linux interpreter is packaged')
    return data


def prepare(args):
    if args.output.exists():
        raise ValueError('use a new output directory for the disposable test appliance')
    probe = static_probe(args.probe)
    spec = importlib.util.spec_from_file_location('hyper_io_images', ROOT / 'scripts/io-vm-images.py')
    images = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(images)
    kernel, ramfs = images.package_payloads(args.package, require_client_resources=True)
    original = gzip.decompress(ramfs.read_bytes())
    # The binary echo probe owns the named port exclusively in this fixture.
    expanded = original + overlay({
        'init': init_script(original), 'console-hotplug': probe,
        'usr/bin/hyper-guest-agent': b'#!/bin/sh\nexit 0\n',
    })
    compressed = gzip.compress(expanded, mtime=0)
    kernel_name = 'Image-' + hashlib.sha256(kernel.read_bytes()).hexdigest()
    ramfs_name = 'initramfs-' + hashlib.sha256(compressed).hexdigest() + '.cpio.gz'

    source = copy.deepcopy(Board.load(args.board).source)
    if source['boot'] != 'qemu-direct':
        raise ValueError('console hotplug acceptance requires QEMU')
    source['io-vm'].pop('network-device', None)
    source['io-vm'].pop('networks', None)
    source['virtual-machines'] = []
    source['files'] = {}
    source['disk']['partitions'] = [part for part in source['disk']['partitions'] if 'content' in part]
    Board.parse(source)

    package = args.output / 'package'
    package.mkdir(parents=True)
    shutil.copyfile(kernel, package / kernel_name)
    (package / ramfs_name).write_bytes(compressed)
    metadata = {'format': 1, 'architecture': 'aarch64', 'platform': 'qemu',
                'client_table_version': 5,
                'kernel': kernel_name, 'initramfs': ramfs_name,
                'initramfs_expanded_size': len(expanded),
                'initramfs_compressed_size': len(compressed)}
    (package / 'boot-artifacts.json').write_text(json.dumps(metadata, indent=2) + '\n')
    images.package_payloads(package)
    (args.output / 'config.json').write_text(json.dumps(source, indent=2) + '\n')
    print(f'Prepared explicit console hotplug test appliance: {package}')


def verify_markers(output):
    positions = []
    for marker in MARKERS:
        position = output.find(marker + b'\n')
        if position < 0:
            raise ValueError(f'missing console acceptance marker: {marker!r}')
        positions.append(position)
    if not positions[0] < min(positions[1:]):
        raise ValueError('empty slot must be observed before system takeover')
    if positions[3:] != sorted(positions[3:]):
        raise ValueError('console discovery and binary rounds are out of order')


def run(args):
    command = [sys.executable, '-B', str(ROOT / 'scripts/run-io-vm.py'), '--qemu', args.qemu,
               '--image', str(args.image), '--initramfs', str(args.initramfs),
               '--disk', str(args.disk), '--board', str(args.board)]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with Session(command, args.log, failures=FAILURES, output_filter=append_console_output) as session:
        transcript = bytearray()
        wanted = set(MARKERS)
        pattern = b'(?:' + b'|'.join(re.escape(marker) for marker in MARKERS) + b')\n'
        limit = time.monotonic() + args.timeout
        while wanted:
            remaining = limit - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'console acceptance incomplete: {wanted!r}')
            result = session.await_text(pattern, timeout=remaining)
            transcript.extend(result)
            wanted = {marker for marker in wanted if marker + b'\n' not in transcript}
        verify_markers(transcript)
        # A prompt may precede the guest probe completion. Send a newline to
        # request a fresh prompt instead of waiting for one already consumed.
        session.send(b'\n')
        session.await_text(rb'hyper-sh\$ ', timeout=90)
        for command in ('touch /data/console-hotplug.txt',
                        "echo CONSOLE-STORAGE-''PASS > /data/console-hotplug.txt",
                        'cat /data/console-hotplug.txt',
                        'rm /data/console-hotplug.txt'):
            session.send(checked(command))
            result = session.await_text(rb'(?m)^HYPER-COMMAND-STATUS=[0-9]+\n', timeout=90)
            session.await_text(rb'hyper-sh\$ ')
            if status(result) != 0:
                raise RuntimeError(f'post-adoption Native storage command failed: {result!r}')
            if command.startswith('cat ') and b'\nCONSOLE-STORAGE-PASS\n' not in result:
                raise RuntimeError(f'post-adoption Native storage content mismatch: {result!r}')
    print(f'PCIe console hotplug, binary transfer, rebind and storage PASS: {args.log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('prepare')
    setup.add_argument('--package', type=Path, required=True)
    setup.add_argument('--probe', type=Path, required=True)
    setup.add_argument('--board', type=Path, default=ROOT / 'boards/qemu.json')
    setup.add_argument('--output', type=Path, required=True)
    setup.set_defaults(action=prepare)
    execute = commands.add_parser('run')
    execute.add_argument('--qemu', required=True)
    for name in ('image', 'initramfs', 'disk', 'board', 'log'):
        execute.add_argument('--' + name, type=Path, required=True)
    execute.add_argument('--timeout', type=float, default=240)
    execute.set_defaults(action=run)
    args = parser.parse_args()
    args.action(args)


if __name__ == '__main__':
    main()
