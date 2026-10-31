#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Revoke an actual Linux I/O VM RX buffer's DMA page, without changing its image.

QMP observes the physical virtio queue. QTest reads evidence and writes only a
test-kernel mailbox; it never modifies guest descriptors, payload or SMMU tables.
Ethernet frames arrive over a private Unix socket, with no external network.
"""
import argparse
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import re
import socket
import struct
import sys
import tempfile
import time

from hyper_test.command import Shell
from hyper_test.journal import Journal
from hyper_test.session import Session, native_command

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('io_launcher', ROOT / 'scripts/run-io-vm.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
NET = '/machine/peripheral/physicalnet/virtio-backend'
SCSI = '/machine/peripheral/physicalscsi/virtio-backend'


def connect(path, session):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(10)
    deadline = time.monotonic() + 10
    try:
        while True:
            try:
                connection.connect(str(path))
                return connection
            except (FileNotFoundError, ConnectionRefusedError):
                session.pump(0.02)
                if session.process.poll() is not None:
                    raise RuntimeError(f'QEMU exited before opening {path}; see {session.logfile}')
                if time.monotonic() >= deadline:
                    raise
    except BaseException:
        connection.close()
        raise


class Monitor:
    def __init__(self, connection, stack):
        self.stream = stack.enter_context(connection.makefile('rwb', buffering=0))
        if 'QMP' not in json.loads(self.stream.readline()):
            raise RuntimeError('missing QMP greeting')
        self.execute('qmp_capabilities')

    def execute(self, command, **arguments):
        self.stream.write(json.dumps({'execute': command, 'arguments': arguments}).encode() + b'\n')
        while True:
            result = json.loads(self.stream.readline())
            if 'error' in result:
                raise RuntimeError(result)
            if 'return' in result:
                return result['return']

    def queue(self, path=NET, queue=0):
        return self.execute('x-query-virtio-queue-status', path=path, queue=queue)

    def head(self):
        element = self.execute('x-query-virtio-queue-element', path=NET, queue=0)
        descriptors = element['descs']
        if len(descriptors) != 1 or descriptors[0]['flags'] != ['write']:
            raise RuntimeError(f'unsupported RX layout: {element}')
        descriptor = descriptors[0]
        address, length = descriptor['addr'], descriptor['len']
        if length < 256 or address // 4096 != (address + length - 1) // 4096:
            raise RuntimeError(f'RX buffer is not contained in one page: {descriptor}')
        return address, length


class Memory:
    def __init__(self, connection, stack):
        self.stream = stack.enter_context(connection.makefile('rwb', buffering=0))

    def request(self, command):
        self.stream.write(command.encode() + b'\n')
        while True:
            response = self.stream.readline().decode().strip()
            if response.startswith('IRQ '):
                continue
            if not response.startswith('OK'):
                raise RuntimeError(f'qtest {command}: {response}')
            return response.split()

    def read(self, address, size):
        data = bytes.fromhex(self.request(f'read {address:#x} {size:#x}')[1].removeprefix('0x'))
        if len(data) != size:
            raise RuntimeError('short physical memory read')
        return data

    def request_revocation(self, mailbox, page):
        self.request(f'writeq {mailbox:#x} {page:#x}')


def wait_queue(session, monitor, previous):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        queue = monitor.queue()
        if queue['used-idx'] != previous:
            return queue
        session.pump(0.01)
    raise TimeoutError('ordinary physical RX DMA did not complete')


def frame(sequence):
    # Unknown EtherType is discarded by Linux AFTER the physical RX DMA. No
    # Linux interface address, DHCP, agent RPC or business guest is required.
    return bytes.fromhex('ffffffffffff024859aabbcc88b5') + (f'HYPER-DMA-{sequence:08d}:'.encode() * 8)


def send_frame(connection, payload):
    connection.sendall(struct.pack('!I', len(payload)) + payload)


def run(args):
    board = launcher.validate_board_disk(args.disk, args.board)
    for key in ('io-device', 'network-device'):
        if board.source['io-vm'][key]['profile'] != 'pci-function':
            raise ValueError('this fixture requires physical PCI storage and networking')
    if any(vm['autostart'] for vm in board.source['virtual-machines']):
        raise ValueError('business guests must not autostart in the DMA fixture')
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hyper-dma-', dir='/tmp') as directory, ExitStack() as stack:
        directory = Path(directory)
        qmp, qtest, net = (directory / name for name in ('qmp', 'qtest', 'net'))
        command = native_command(args.qemu, args.image, args.initramfs)
        machine = command.index('-machine') + 1
        command[machine] = launcher.machine_arguments(board, command[machine])
        command += ['-qmp', f'unix:{qmp},server=on,wait=off',
                    '-qtest', f'unix:{qtest},server=on,wait=off', '-qtest-log', '/dev/null',
                    '-drive', 'if=none,id=physicaldisk,format=raw,snapshot=on,file='
                    + str(args.disk.resolve()).replace(',', ',,'),
                    '-device', launcher.storage_controller(board),
                    '-device', 'scsi-hd,drive=physicaldisk,bus=physicalscsi.0,scsi-id=0,lun=0']
        network = launcher.network_arguments(board)
        network[1] = f'stream,id=physicalnet,server=on,addr.type=unix,addr.path={net}'
        command += network
        session = stack.enter_context(Session(command, args.log))
        monitor = Monitor(stack.enter_context(connect(qmp, session)), stack)
        memory = Memory(stack.enter_context(connect(qtest, session)), stack)
        ethernet = stack.enter_context(connect(net, session))
        ready = session.observe(rb'DMA-REVOKE: ready control=(0x[0-9a-f]+) sid=16', timeout=180)
        mailbox = int(re.search(rb'control=(0x[0-9a-f]+)', ready)[1], 16)
        if args.before_adoption:
            session.observe(rb'HypeR I/O: network default ready', timeout=180)
        else:
            session.await_text(rb'hyper-sh\$ ', timeout=180)
            Shell(session).check('echo dma-before > /data/dma-revoke.txt')
        # Warm production translations with exact bytes, then select another
        # posted buffer on a page which demonstrably received successful DMA.
        warmed = set()
        header_length = None
        queue = monitor.queue()
        for sequence in range(1, queue['vring-num'] * 4 + 1):
            address, length = monitor.head()
            page = address & ~4095
            if page in warmed:
                break
            payload = frame(sequence)
            previous = monitor.queue()['used-idx']
            send_frame(ethernet, payload)
            wait_queue(session, monitor, previous)
            received = memory.read(address, length)
            offset = received.find(payload)
            if offset not in (10, 12, 20):
                raise RuntimeError('successful RX did not contain the test frame')
            if header_length is not None and header_length != offset:
                raise RuntimeError('virtio header length changed during RX')
            header_length = offset
            warmed.add(page)
        else:
            raise RuntimeError('no posted RX buffer reused a warmed page')
        print(f'Positive control: {len(warmed)} DMA-written pages; selecting {address:#x}', flush=True)
        denied_payload = frame(0xDEAD)
        # Linux may use the unused RX tail for skb metadata after completion,
        # including when QEMU completes a denied transfer via a bounce buffer.
        # Compare EVERY byte this DMA could write (header plus Ethernet frame),
        # not driver-owned padding beyond the transfer. No CPU mapping is revoked.
        written_length = header_length + len(denied_payload)
        if written_length > length:
            raise RuntimeError('test frame exceeds the selected RX buffer')
        monitor.execute('stop')
        try:
            before = memory.read(address, written_length)
            used = monitor.queue()['used-idx']
            memory.request_revocation(mailbox, page)
        finally:
            monitor.execute('cont')
        session.observe(f'DMA-REVOKE: unmapped iova={page:#x}; CMD_SYNC complete; backing retained'.encode())
        # No inbound frames were sent while the kernel changed the mapping.
        if monitor.queue()['used-idx'] != used or monitor.head() != (address, length):
            raise RuntimeError('RX queue changed before the negative control')
        marker = session.mark()
        send_frame(ethernet, denied_payload)
        fault = session.observe(rb'SMMUv3 quarantined SID 16 domain [^\n]+: Translation, address (0x[0-9a-f]+), read=false',
                                timeout=15, after=marker)
        fault_address = int(re.search(rb'address (0x[0-9a-f]+)', fault)[1], 16)
        if fault_address != address:
            raise RuntimeError(f'fault did not name the selected RX buffer: {fault!r}')
        if args.before_adoption:
            # This variant must actually exercise retained evidence, not merely
            # a second notification after formal runtime startup.
            serial = args.log.read_bytes()
            adopted = serial.find(b'HypeR io-runtime: running backend adopted')
            if adopted >= 0 and adopted < serial.find(fault):
                raise RuntimeError('fault arrived after adoption; early-fault coverage is inconclusive')
        after = memory.read(address, written_length)
        if after != before:
            args.log.with_suffix('.buffer.json').write_text(json.dumps({
                'address': address, 'before': before.hex(), 'after': after.hex(),
                'differences': [i for i, (a, b) in enumerate(zip(before, after)) if a != b],
            }, indent=2) + '\n')
            raise RuntimeError('denied RX modified its retained buffer')
        # The production fault worker quarantines only the offending SID. A
        # separate physical virtio-SCSI function must still perform real DMA.
        if args.before_adoption:
            session.await_text(rb'hyper-sh\$ ', timeout=180)
        shell = Shell(session)
        previous = monitor.queue(SCSI, 2)['used-idx']
        shell.check('echo dma-after > /data/dma-revoke.txt')
        result = shell.check('cat /data/dma-revoke.txt')
        if b'dma-after' not in result or monitor.queue(SCSI, 2)['used-idx'] == previous:
            raise RuntimeError('unrelated physical storage did not complete new I/O')
        for sequence in range(3):
            send_frame(ethernet, frame(0xBEEF + sequence))
        session.pump(0.5)
        if memory.read(address, written_length) != before:
            raise RuntimeError('quarantined network stream modified the buffer')
        journal = Journal(shell)
        records = journal.message('/io-runtime', 'DMA fault: device=network', stream='stderr')
        reports = [record for record in records if 'DMA fault:' in record['text']]
        if len(reports) != 1:
            raise RuntimeError(f'expected one device-scoped journal report: {reports}')
        report = reports[0]['text']
        if not all(value in report for value in ('scope=Stream', 'sid=16', 'code=0x10',
                                                f'address={address:#x}', 'access=write',
                                                'action=report-only')):
            raise RuntimeError(f'incorrect journal fault attribution: {report}')
        if any('observation unavailable' in record['text'] for record in records):
            raise RuntimeError(f'fault observer failed: {records}')
        state = shell.check('vmm list')
        name = board.source['io-vm']['name'].encode()
        if re.search(rb'(?m)^' + re.escape(name) + rb'\s+running\b', state) is None:
            raise RuntimeError(f'I/O VM stopped after notification: {state!r}')
        evidence = {'journal_report': report, 'io_vm_running': True,
                    'before_adoption': args.before_adoption,
                    'revoked_page': page, 'buffer': address, 'length': length,
                    'verified_write_bytes': written_length,
                    'warmed_pages': len(warmed), 'fault': fault.decode(),
                    'unchanged': True, 'storage_dma_after_fault': True}
        args.log.with_suffix('.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print('I/O VM DMA revocation, real SMMU fault, retained bytes and storage isolation passed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--before-adoption', action='store_true',
                        help='raise the fault during bootstrap and require journal replay after adoption')
    for name in ('image', 'initramfs', 'disk', 'board', 'log'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
