#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Verify the ordinary shell survives idle I/O startup without disk writes."""

import argparse
import hashlib
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--initramfs', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix=args.log.stem + '-', dir=args.log.parent))
    disk = evidence / 'storage.img'
    # Nonzero preexisting content catches both truncation and guest test writes.
    with disk.open('xb') as stream:
        stream.write(b'\xa5' * 4096)
        stream.truncate(8 * 1024 * 1024)
    before = digest(disk)
    launcher = Path(__file__).resolve().parents[2] / 'scripts/run-io-vm.py'
    command = [sys.executable, '-B', str(launcher), '--qemu', args.qemu,
               '--image', str(args.image), '--initramfs', str(args.initramfs), '--disk', str(disk)]
    output = bytearray()
    with args.log.open('wb') as log, selectors.DefaultSelector() as selector:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        try:
            selector.register(child.stdout, selectors.EVENT_READ)
            limit = time.monotonic() + 120
            command_bytes = b"echo IO-SHELL-" + b"RESPONSIVE\n"
            sent = 0
            next_key = None
            response_after = None
            success_at = None
            while time.monotonic() < limit:
                for key, _ in selector.select(0.05):
                    data = os.read(key.fd, 65536)
                    if not data:
                        raise RuntimeError('QEMU exited before standby acceptance')
                    log.write(data)
                    log.flush()
                    output.extend(data)
                if any(marker in output for marker in (
                        b'[HypeR bootstrap stage 1][io-runtime] failed:', b'HypeR io-runtime: failed:',
                        b'Kernel panic', b'HypeR KERNEL PANIC',
                        b'HypeR: fatal', b'bootstrap failed', b'IO-VM-SMOKE:',
                        b'HypeR business disk:')):
                    raise RuntimeError('boot failed or unexpectedly ran the destructive fixture')
                now = time.monotonic()
                if (next_key is None and b'[HypeR bootstrap stage 1][io-runtime] ready;' in output
                        and b'hyper-sh$' in output):
                    next_key = now
                if next_key is not None and sent < len(command_bytes) and now >= next_key:
                    if sent == len(command_bytes) - 1:
                        response_after = len(output)
                    child.stdin.write(command_bytes[sent:sent + 1])
                    child.stdin.flush()
                    sent += 1
                    next_key = now + 0.12
                # Ignore the echoed command itself: require a distinct output line.
                if response_after is not None and b'\nIO-SHELL-RESPONSIVE\n' in output[response_after:].replace(b'\r\n', b'\n'):
                    if success_at is None:
                        success_at = now
                if success_at is not None and now - success_at >= 5:
                    break
                if child.poll() is not None:
                    raise RuntimeError('QEMU terminated during idle observation')
            else:
                raise TimeoutError('standby boot or interactive shell verification timed out')
        except Exception:
            sys.stderr.write(output[-16384:].decode(errors='replace'))
            raise
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
    if digest(disk) != before:
        raise RuntimeError('ordinary standby boot modified the physical disk')
    print(f'I/O standby and interactive shell acceptance passed: {args.log}')
    print(f'Existing disk preserved: {disk}')


if __name__ == '__main__':
    main()
