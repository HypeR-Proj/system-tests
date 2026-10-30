#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
"""Qualify guest networking through the QEMU I/O VM uplink.

Use prepare to derive a two-guest fixture from the ordinary board, then build
its disposable disk and matching Native initramfs. Both guests must include
Alpine's virtio_net module. This functional
test has no throughput thresholds. HTTP uses localhost unless --external-url
also requests an outgoing smoke test. The disk guest writes one small
persistent proof file in /root.
"""
import argparse
import copy
from contextlib import contextmanager
from functools import partial
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import shlex
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from board_config import Board
from hyper_test.guest_console import append_console_output
from hyper_test.session import Session

GUEST_BOOT_TIMEOUT_SECONDS = 300


def prepare(args):
    """Add a network-only peer to the normal disk+network Alpine deployment."""
    board = Board.load(args.board)
    source = copy.deepcopy(board.source)
    if args.physical_transport == 'mmio':
        source['io-vm']['io-device'] = {'profile': 'virtio-mmio-scsi', 'compatible': 'virtio,mmio'}
        source['io-vm']['network-device'] = {'profile': 'virtio-mmio-net', 'compatible': 'virtio,mmio'}
    else:
        source['io-vm']['io-device'] = {'profile': 'pci-function', 'pci-id': '1af4:1048'}
        source['io-vm']['network-device'] = {'profile': 'pci-function', 'pci-id': '1af4:1041'}
    guests = source['virtual-machines']
    if (source['boot'] != 'qemu-direct' or len(guests) != 1
            or 'disk' not in guests[0] or 'network' not in guests[0]):
        raise ValueError('prepare requires a QEMU board with one disk+network guest')
    if args.output.resolve() == args.board.resolve():
        raise ValueError('the network fixture must not replace the input board')
    guests[0]['configuration']['virtio-transport'] = args.virtio_transport
    guests[0]['autostart'] = False
    peer = copy.deepcopy(guests[0])
    peer['name'] = 'alpine-net'
    peer.pop('disk', None)
    config = peer['configuration']
    config['bootargs'] = re.sub(r'(?<!\S)hyper\.root=\S+\s*', '', config['bootargs']).rstrip()
    mac = peer['network']['mac'].split(':')
    mac[-1] = f'{(int(mac[-1], 16) + 1) % 256:02x}'
    peer['network']['mac'] = ':'.join(mac)
    guests.append(peer)
    fixture = Board.parse(source)
    test_guests(fixture)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(source, indent=2) + '\n')
    print(f'Prepared disk+network and network-only guests: {args.output}')


@contextmanager
def payload_server():
    """Download and verify an upload of one bounded, non-MTU-aligned object."""
    # BusyBox wget handles POST data as a C string. Random ASCII keeps its
    # --post-file upload intact; packet tests also exercise arbitrary bytes.
    size = 256 * 1024 + 137
    payload = secrets.token_hex((size + 1) // 2).encode()[:size]

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(60)

        def do_GET(self):
            if self.path != '/payload':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if self.path != '/payload':
                self.send_error(404)
                return
            length = self.headers.get('Content-Length', '')
            if length != str(len(payload)):
                # Drain bounded invalid uploads before closing: otherwise macOS
                # can reset the socket while the client is still sending and
                # hide the HTTP rejection behind a transport error.
                if length.isdecimal() and int(length) <= len(payload):
                    self.rfile.read(int(length))
                self.send_error(400, 'unexpected payload length')
                return
            received = self.rfile.read(len(payload))
            if received != payload:
                self.send_error(400, 'payload mismatch')
                return
            receipt = hashlib.sha256(received).hexdigest().encode() + b'\n'
            self.send_response(200)
            self.send_header('Content-Length', str(len(receipt)))
            self.end_headers()
            self.wfile.write(receipt)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield (f'http://10.0.2.2:{server.server_port}/payload',
                   hashlib.sha256(payload).hexdigest())
        finally:
            server.shutdown()
            thread.join(timeout=3)


def test_guests(board):
    """Require one disk+network guest and one network-only guest, both stopped."""
    if board.source['boot'] != 'qemu-direct' or 'network-device' not in board.source['io-vm']:
        raise ValueError('network acceptance requires a QEMU board with a physical uplink')
    guests = board.source['virtual-machines']
    disk = [vm for vm in guests if 'disk' in vm and 'network' in vm]
    network = [vm for vm in guests if 'disk' not in vm and 'network' in vm]
    if len(disk) != 1 or len(network) != 1 or any(vm['autostart'] for vm in guests):
        raise ValueError('require one disk+network and one network-only guest, without autostart')
    return disk[0], network[0]


def release_acknowledgements(data):
    """Count complete backend proofs across the stop command's bounded output."""
    output = bytearray()
    append_console_output(output, data)
    # GuestLog forwards partial Linux lines. Only these exact CLI fragments can
    # interleave while stop() waits quietly; unrelated text remains a failure.
    output = output.replace(b'accepted\n', b'').replace(b'hyper-sh$ ', b'')
    return len(re.findall(
        rb'HypeR IO VM: HypeR I/O \[[^\]\n]+\]: reply RELEASE_MEMORY: ok\n', output))


class Scenario:
    def __init__(self, session, url, digest, expect_tx_offload=None):
        self.session = session
        self.url = shlex.quote(url)
        self.digest = digest
        self.expect_tx_offload = expect_tx_offload
        self.counter = 0
        self.releases = 0
        self.storage_proof = secrets.token_hex(16)

    def send(self, value):
        # Pace commands through the bounded serial queues, including guest RX.
        for offset in range(0, len(value), 32):
            self.session.send(value[offset:offset + 32])
            self.session.pump(0.02)

    def marker(self, owner):
        self.counter += 1
        marker = f'NETWORK-{owner}-{self.counter}'.encode()
        # The serial input echo must not satisfy an output fence.
        quoted = marker.replace(b'-', b"-''", 1)
        return marker, quoted

    def native(self, command):
        marker, quoted = self.marker('NATIVE')
        self.send(command.encode() + b'\necho ' + quoted + b'\n')
        result = self.session.await_text(re.escape(marker) + rb'\n', timeout=180)
        self.session.await_text(rb'hyper-sh\$ ')
        if b'vmm:' in result or b'sh: command' in result:
            raise RuntimeError(f'Native command failed: {result!r}')
        return result

    def guest(self, command):
        marker, quoted = self.marker('GUEST')
        # A brace group retains netif/device variables between shell commands.
        self.send(b'{ ' + command.encode() + b"; }; rc=$?; printf '\\n" + quoted
                  + b":%s\\n' \"$rc\"\n")
        result = self.session.await_text(re.escape(marker) + rb':([0-9]+)\n', timeout=180)
        status = re.search(re.escape(marker) + rb':([0-9]+)\n$', result)
        if int(status[1]) != 0:
            raise RuntimeError(f'guest command failed ({command}): {result!r}')
        self.session.await_text(rb'~ # ')
        return result

    def state(self, name, wanted):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = self.native(f'vmm status {name}')
            if re.search(rb'\b' + re.escape(name.encode()) + rb'\s+' + wanted + rb'\b', result):
                return
            if b'failed' in result:
                raise RuntimeError(f'VM failed: {result!r}')
            self.session.pump(0.2)
        raise TimeoutError(f'{name} did not reach {wanted.decode()}')

    @contextmanager
    def console(self, vm):
        name = vm['name']
        self.state(name, b'running')
        self.send(f'vmm console {name}\n'.encode())
        self.session.await_text(re.escape(f'Connected to {name}.'.encode()))
        self.send(b'\n')
        # Match the SMP boot allowance; network commands keep their own limits.
        self.session.await_text(rb'~ # ', timeout=GUEST_BOOT_TIMEOUT_SECONDS)
        try:
            yield
        finally:
            self.send(b'\x1dd')
            self.session.await_text(rb'hyper-sh\$ ')

    def interface(self, vm):
        mac = shlex.quote(vm['network']['mac'])
        self.guest('netif=; for path in /sys/class/net/*; do '
                   f'if [ "$(cat "$path/address")" = {mac} ]; then netif=${{path##*/}}; fi; '
                   'done; test -n "$netif" && test "$netif" != lo')

    def configure(self, vm):
        self.guest('modprobe virtio_mmio && modprobe virtio_pci && modprobe virtio_net')
        self.interface(vm)
        if vm['configuration'].get('virtio-transport', 'mmio') == 'pci':
            self.guest('test "$(cat "/sys/class/net/$netif/device/../vendor")" = 0x1af4 && '
                       'test "$(cat "/sys/class/net/$netif/device/../device")" = 0x1041')
            if 'disk' in vm:
                self.guest('test "$(cat /sys/bus/pci/devices/0000:00:01.0/device)" = 0x1048')

        self.guest('ip link set dev "$netif" up && '
                   'udhcpc -n -q -t 10 -T 3 -i "$netif"')

    def automatic_network(self, vm):
        # Boot must supply the address, route and DNS without test-side setup.
        # Also require the client to remain alive for later lease renewals.
        self.interface(vm)
        self.guest('attempts=0; until '
                   'ip -4 addr show dev "$netif" scope global | grep -q "inet " && '
                   'ip route show default dev "$netif" | grep -q "^default " && '
                   'grep -q "^nameserver " /etc/resolv.conf; do '
                   'attempts=$((attempts + 1)); test "$attempts" -lt 60 || break; '
                   'sleep 1; done; test "$attempts" -lt 60 && '
                   'test -s "/run/udhcpc-$netif.pid" && '
                   'kill -0 "$(cat "/run/udhcpc-$netif.pid")"')

    def transfer(self):
        result = self.guest("printf 'VIRTIO-''FEATURES=%s\\n' "
                            '"$(cat "/sys/class/net/$netif/device/features")"')
        # Linux prints negotiated bits least significant first. Check the
        # runtime state, not merely the model's advertised capabilities.
        match = re.search(rb'VIRTIO-FEATURES=([01]{64,})\n', result)
        if not match:
            raise RuntimeError(f'missing negotiated virtio features: {result!r}')
        features = int(match[1][::-1], 2)
        offloads = (1 << 0) | (1 << 11) | (1 << 12)
        if self.expect_tx_offload is not None:
            wanted = offloads if self.expect_tx_offload == 'enabled' else 0
            if features & offloads != wanted:
                raise RuntimeError(f'unexpected TX offload features: {features:#x}')
        if features & ((1 << 1) | (1 << 7) | (1 << 8) | (1 << 9) | (1 << 15)):
            raise RuntimeError(f'unsupported guest RX offloads: {features:#x}')
        # Remove the previous object so an unsuccessful download cannot pass.
        self.guest(f'rm -f /tmp/hyper-network-payload && '
                   f'wget -T 60 -q -O /tmp/hyper-network-payload {self.url} && '
                   f'test "$(sha256sum /tmp/hyper-network-payload | cut -d\' \' -f1)" '
                   f'= {self.digest}')
        # TCP guest TX must reach the peer intact, including the odd final
        # segment. RX alone would not detect a backend dropping GSO metadata.
        self.guest('rm -f /tmp/hyper-network-receipt && '
                   'wget -T 60 -q --post-file=/tmp/hyper-network-payload '
                   f'-O /tmp/hyper-network-receipt {self.url} && '
                   f'test "$(cat /tmp/hyper-network-receipt)" = {self.digest}')

    def external_transfer(self, url):
        # Explicitly requested smoke coverage for DNS and an external HTTP
        # path; localhost checksum transfers remain the default oracle.
        self.guest('rm -f /tmp/hyper-network-external && '
                   f'wget -T 60 -q -O /tmp/hyper-network-external {shlex.quote(url)} && '
                   'test -s /tmp/hyper-network-external')

    def storage(self, vm, *, create=False):
        if 'disk' not in vm:
            self.guest('test ! -e /sys/block/sda && '
                       '! grep -qx 0x00000008 /sys/bus/virtio/devices/*/device')
            return
        self.guest('test -b /dev/sda && grep -q "/dev/sda / ext4 rw" /proc/mounts')
        proof = '/root/hyper-network-storage-proof'
        if create:
            self.guest(f'printf %s {self.storage_proof} > {proof} && sync')
        self.guest(f'test "$(cat {proof})" = {self.storage_proof}')

    def reset_network(self, vm):
        self.interface(vm)
        # Retire the boot client before intentionally removing its interface;
        # the reset fixture then acquires a fresh lease after driver rebind.
        self.guest('kill "$(cat "/run/udhcpc-$netif.pid")" && '
                   'device=$(readlink -f "/sys/class/net/$netif/device") && '
                   'device=${device##*/} && test -n "$device" && '
                   'printf %s "$device" > /sys/bus/virtio/drivers/virtio_net/unbind && '
                   'test ! -e "/sys/class/net/$netif" && '
                   'printf %s "$device" > /sys/bus/virtio/drivers/virtio_net/bind')
        self.configure(vm)
        self.transfer()
        self.storage(vm)

    def stop(self, vm):
        # Avoid a fence command and status polling while Linux forwards its
        # release proof: their output can split an otherwise valid log record.
        self.send(f'vmm stop {vm["name"]}\n'.encode())
        response = self.session.await_text(
            rb'accepted\n|vmm:[^\n]*\n|sh: command[^\n]*\n', timeout=180)
        if b'vmm:' in response or b'sh: command' in response:
            raise RuntimeError(f'Native stop failed: {response!r}')
        self.session.await_text(rb'hyper-sh\$ ')
        self.retired(self.releases + 1)
        self.releases += 1
        self.state(vm['name'], b'stopped')

    def retired(self, expected, timeout=180):
        # The manager can report stopped before the I/O VM has acknowledged
        # release. Include earlier stop/start rounds even though their serial
        # output has already been consumed by other command expectations.
        deadline = time.monotonic() + timeout
        while True:
            self.session.pump(0.05)
            completed = release_acknowledgements(Path(self.session.logfile).read_bytes())
            if completed == expected:
                return
            if completed > expected:
                raise RuntimeError(f'unexpected memory release count: {completed}, wanted {expected}')
            if time.monotonic() >= deadline:
                raise TimeoutError(f'memory release acknowledgements: {completed}/{expected}')


def run(args):
    if args.external_url and not args.external_url.startswith('http://'):
        raise ValueError('--external-url requires an HTTP URL to a small public file')
    board = Board.load(args.board)
    guests = test_guests(board)
    command = [sys.executable, '-B', str(ROOT / 'scripts/run-io-vm.py'),
               '--qemu', args.qemu, '--image', str(args.image),
               '--initramfs', str(args.initramfs), '--disk', str(args.disk),
               '--board', str(args.board)]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    failures = (b'HypeR: fatal', b'Kernel panic', b'HypeR KERNEL PANIC',
                b'bootstrap failed', b'quiescence failed', b'HypeR I/O: services failed',
                b'HypeR I/O: service failed', b'HypeR vm-runtime: failed:',
                b'[vmm] virtual machine disconnected')
    with payload_server() as (url, digest), Session(
            command, args.log, failures=failures,
            output_filter=partial(append_console_output, filter_guest_logs=True)) as session:
        scenario = Scenario(session, url, digest, args.expect_tx_offload)
        session.await_text(rb'\[HypeR bootstrap stage 1\]\[io-runtime\] ready; bootstrap mounts complete\n', timeout=180)
        if board.source['io-vm']['io-device']['profile'] == 'pci-function':
            for marker in (b'SMMUv3 stage-2 active:', b'physical PCI DMA domain installed',
                           b'PCI function 1af4:1048 prepared', b'PCI function 1af4:1041 prepared'):
                # Interactive output removes kernel records. Admission evidence
                # must come from the unfiltered archive, even after readiness.
                session.observe(re.escape(marker), timeout=1)
        # Storage readiness precedes formal terminal ownership. Commands sent
        # before the shell prompt can be discarded by the session's input reset.
        session.await_text(rb'hyper-sh\$ ', timeout=180)
        deadline = time.monotonic() + 90
        while True:
            listing = scenario.native('vmm list')
            if all(re.search(rb'\b' + re.escape(vm['name'].encode()) + rb'\s+stopped\b', listing)
                   for vm in guests):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('init did not provision both network guests')
            session.pump(0.2)
        # Configuration/FDT tests check owner 0's management-only assignment.
        # Runtime forwarding and later client reset exercise the adopted policy
        # without keeping bootstrap configuration files alive for inspection.

        # Bring each guest to readiness before booting the next. Concurrent
        # Linux initialization saturates cross-architecture TCG workers and can
        # exhaust the boot deadline before either console becomes interactive.
        # Both guests stay alive for all transfer/reset/retirement assertions.
        for vm in guests:
            scenario.native(f'vmm start {vm["name"]}')
            with scenario.console(vm):
                scenario.automatic_network(vm)
        for vm in guests:
            with scenario.console(vm):
                scenario.interface(vm)
                scenario.transfer()
                if args.external_url:
                    scenario.external_transfer(args.external_url)
                scenario.storage(vm, create=True)
        for vm in guests:
            with scenario.console(vm):
                scenario.reset_network(vm)
        # Both clients remain usable after the other's endpoint reset.
        for vm in guests:
            with scenario.console(vm):
                scenario.transfer()
        for vm in guests:
            scenario.stop(vm)
            scenario.native(f'vmm start {vm["name"]}')
            with scenario.console(vm):
                scenario.automatic_network(vm)
                scenario.transfer()
                scenario.storage(vm)
        for vm in guests:
            scenario.stop(vm)
        scenario.retired(2 * len(guests))
        scenario.native('echo NETWORK-LIFECYCLE-PASS')
    print(f'Guest automatic DHCP, bidirectional HTTP checksum, reset/rebind, stop/start and memory release passed: {args.log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    fixture = actions.add_parser('prepare', help='derive the two-guest acceptance configuration')
    fixture.add_argument('--board', type=Path, required=True)
    fixture.add_argument('--output', type=Path, required=True)
    fixture.add_argument('--virtio-transport', choices=('mmio', 'pci'), default='pci')
    fixture.add_argument('--physical-transport', choices=('pci', 'mmio'), default='pci')
    fixture.set_defaults(handler=prepare)
    execute = actions.add_parser('run', help='verify a built acceptance fixture')
    execute.add_argument('--qemu', default='qemu-system-aarch64')
    execute.add_argument('--board', type=Path, required=True)
    execute.add_argument('--external-url', help='optionally fetch a small public HTTP file once per guest')
    execute.add_argument('--expect-tx-offload', choices=('enabled', 'disabled'),
                         help='require the negotiated TX checksum/TSO state on every transfer')
    for name in ('image', 'initramfs', 'disk', 'log'):
        execute.add_argument('--' + name, type=Path, required=True)
    execute.set_defaults(handler=run)
    args = parser.parse_args()
    args.handler(args)


if __name__ == '__main__':
    main()
