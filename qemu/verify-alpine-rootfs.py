#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
"""Verify Alpine root filesystem persistence across the complete I/O VM path."""
import argparse
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time


def run(args):
    command = [sys.executable, '-B', 'scripts/run-io-vm.py', '--qemu', args.qemu,
               '--image', str(args.image), '--initramfs', str(args.initramfs),
               '--disk', str(args.disk), '--board', str(args.board)]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open('wb') as log, selectors.DefaultSelector() as selector:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        selector.register(child.stdout, selectors.EVENT_READ)
        pending = bytearray()

        def expect(pattern, timeout=180):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                match = re.search(pattern, pending)
                if match:
                    result = bytes(pending[:match.end()])
                    del pending[:match.end()]
                    return result
                for key, _ in selector.select(0.2):
                    data = os.read(key.fd, 65536)
                    if not data:
                        raise RuntimeError('QEMU console closed')
                    log.write(data); log.flush()
                    pending.extend(data.replace(b'\r', b''))
                    if any(marker in pending for marker in (b'Kernel panic', b'HypeR KERNEL PANIC',
                            b'job control turned off', b'ALPINE-FAIL')):
                        raise RuntimeError(bytes(pending[-4096:]))
            raise TimeoutError(f'{pattern!r}: {bytes(pending[-4096:])!r}')

        def send(value):
            # Pace long shell commands through the bounded serial RX queues.
            # A pipe write is otherwise faster than a physical terminal.
            for offset in range(0, len(value), 32):
                child.stdin.write(value[offset:offset + 32]); child.stdin.flush()
                time.sleep(0.02)

        counter = 0

        def native(command):
            nonlocal counter
            counter += 1
            marker = f'ALPINE-NATIVE-{counter}'.encode()
            send(command.encode() + b'\necho ALPINE-\'\'NATIVE-' + str(counter).encode() + b'\n')
            result = expect(marker + rb'\n')
            expect(rb'hyper-sh\$ ')
            if b'vmm:' in result or b'sh: command' in result:
                raise RuntimeError(result)
            return result

        def state(wanted):
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if re.search(rb'\balpine\s+' + wanted + rb'\b', native('vmm status alpine')):
                    return
                time.sleep(0.2)
            raise TimeoutError(f'Alpine state {wanted!r}')

        def attach():
            state(b'running')
            send(b'vmm console alpine\n')
            expect(rb'HypeR guest: Linux userspace is running\n')
            expect(rb'~ # ')

        def guest(command, marker):
            # Split the result marker in shell syntax so echo cannot satisfy it.
            send(command.encode() + b" && echo " + marker.replace(b'-', b"-''", 1)
                 + b" || echo ALPINE-''FAIL\n")
            expect(rb'\n' + marker + rb'\n')
            expect(rb'~ # ')

        try:
            expect(rb'\[HypeR bootstrap stage 1\]\[io-runtime\] ready; bootstrap mounts complete\n')
            expect(rb'hyper-sh\$ ')
            deadline = time.monotonic() + 60
            while not re.search(rb'\balpine\s+stopped\b', native('vmm list')):
                if time.monotonic() >= deadline:
                    raise TimeoutError('init did not provision Alpine')
                time.sleep(0.2)
            native('vmm start alpine'); attach()
            guest("apk --version && test -f /etc/alpine-release && "
                  "grep -q '/dev/sda / ext4 rw' /proc/mounts && "
                  "dd if=/dev/urandom of=/root/io-proof bs=4096 count=256 && "
                  "sha256sum /root/io-proof > /root/io-proof.sha256 && sync",
                  b'ALPINE-WRITE-PASS')
            send(b'\x1dd'); expect(rb'hyper-sh\$ ')
            native('vmm stop alpine'); state(b'stopped')
            native('vmm start alpine'); attach()
            guest('sha256sum -c /root/io-proof.sha256 && test "$(wc -c < /root/io-proof)" -eq 1048576',
                  b'ALPINE-PERSISTENCE-PASS')
            send(b'\x1dd'); expect(rb'hyper-sh\$ ')
            native('vmm stop alpine'); state(b'stopped')
        finally:
            child.stdin.close()
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
            child.stdout.close()
    print(f'Alpine ext4 root via I/O VM: 1 MiB write, sync, restart and checksum passed: {args.log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', default='qemu-system-aarch64')
    for name in ('image', 'initramfs', 'disk', 'board', 'log'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
