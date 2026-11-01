#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Capture low-stack call chains during a bounded diagnostic QEMU boot.

This deliberately perturbs interrupt timing. It supplements a failed acceptance
run, never replaces its result, and never writes the original board disk.
The geometry is restricted to the AArch64 48-bit, 16 KiB CI configuration.
"""

import argparse
import bisect
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
ARENA = 0xffffff8000200000
STRIDE = 0x41000
GUARD = 4096
STACK_SIZE = 16384
WATCH_SIZE = 4096
SLOT_COUNT = 256


class Remote:
    """The small synchronous subset of QEMU's GDB remote protocol used here."""

    def __init__(self, stream):
        self.stream = stream

    def read(self, count, on_idle=None):
        data = bytearray()
        while len(data) < count:
            try:
                part = self.stream.recv(count - len(data))
            except socket.timeout:
                if on_idle is None:
                    raise
                on_idle()
                continue
            if not part:
                raise EOFError('QEMU closed its GDB socket')
            data.extend(part)
        return bytes(data)

    def receive(self, on_idle=None):
        while self.read(1, on_idle) != b'$':
            pass  # Ignore acknowledgements preceding a response packet.
        packet = bytearray()
        while (byte := self.read(1, on_idle)) != b'#':
            packet.extend(byte)
            if len(packet) > 262144:
                raise ValueError('oversized GDB response')
        checksum = int(self.read(2, on_idle), 16)
        if sum(packet) % 256 != checksum:
            raise ValueError('GDB response checksum mismatch')
        self.stream.sendall(b'+')
        return packet.decode('ascii')

    def send(self, command):
        payload = command.encode('ascii')
        self.stream.sendall(b'$' + payload + b'#' + f'{sum(payload) % 256:02x}'.encode())

    def request(self, command):
        self.send(command)
        return self.receive()

    def resume(self, deadline, finished):
        """Wait for a stop, including when an acknowledgement arrives first."""
        interrupted = None

        def idle():
            nonlocal interrupted
            now = time.monotonic()
            if interrupted is not None:
                if now - interrupted >= 5:
                    raise TimeoutError('QEMU did not acknowledge diagnostic interruption')
            elif finished() or now >= deadline:
                self.stream.sendall(b'\x03')
                interrupted = now

        previous = self.stream.gettimeout()
        self.stream.settimeout(.1)
        try:
            self.send('vCont;c')
            return self.receive(idle)
        finally:
            self.stream.settimeout(previous)

    def watch(self, address, enable):
        answer = self.request(f'{"Z" if enable else "z"}2,{address:x},{WATCH_SIZE:x}')
        if answer != 'OK':
            raise ValueError(f'QEMU rejected stack watchpoint: {answer}')


def stack_bounds(sp):
    if sp < ARENA:
        return None
    slot, offset = divmod(sp - ARENA, STRIDE)
    if slot >= SLOT_COUNT or not GUARD <= offset < GUARD + STACK_SIZE:
        return None
    bottom = ARENA + slot * STRIDE + GUARD
    return bottom, bottom + STACK_SIZE


def check_boot_layout(serial):
    # A config file alone cannot prove which image was actually booted. Refuse
    # stale artifacts before interpreting SPs with the configured geometry.
    stacks = re.search(rb'guarded kernel stacks: thread (\d+) KiB,', serial)
    if stacks and int(stacks[1]) != STACK_SIZE // 1024:
        raise ValueError(f'booted image has {int(stacks[1])} KiB stacks; expected 16 KiB')
    width = re.search(rb'AArch64 address space: (\d+)-bit VA', serial)
    if width and int(width[1]) != 48:
        raise ValueError('booted image does not use 48-bit AArch64 addressing')


def unwind(remote, fp, sp, top):
    frames = []
    # Only follow aligned, monotonically increasing frame pointers in this
    # stopped thread's stack. User FPs and a corrupt chain are never trusted.
    while len(frames) < 64 and fp % 16 == 0 and sp <= fp <= top - 16:
        reply = remote.request(f'm{fp:x},10')
        if reply.startswith('E'):
            break
        data = bytes.fromhex(reply)
        if len(data) != 16:
            raise ValueError('short GDB frame response')
        parent, address = struct.unpack('<QQ', data)
        frames.append({'fp': fp, 'return_address': address})
        if parent <= fp:
            break
        fp = parent
    return frames


def symbols(elf):
    tool = os.environ.get('LLVM_NM') or shutil.which('llvm-nm')
    if not tool:
        raise ValueError('llvm-nm is required to symbolize the matching kernel ELF')
    result = subprocess.run([tool, '-n', '-C', str(elf)], check=True,
                            capture_output=True, text=True)
    return [(int(match[1], 16), match[2]) for line in result.stdout.splitlines()
            if (match := re.fullmatch(r'([0-9a-fA-F]+) [tT] (.*)', line))]


def symbolize(captures, table, base):
    addresses = [address for address, _ in table]
    for capture in captures:
        for frame in [{'pc': capture['pc']}, *capture['frames']]:
            pc = frame['pc'] if 'pc' in frame else frame['return_address'] - 4
            relative = pc - base
            index = bisect.bisect_right(addresses, relative) - 1
            frame['relative_pc'] = relative
            if index >= 0:
                frame['symbol'] = table[index][1]
                frame['symbol_offset'] = relative - table[index][0]
            if 'pc' in frame:
                capture['instruction'] = frame


def select_fixture(fixtures, selected=None):
    def complete(path):
        return all((path / name).is_file()
                   for name in ('bootstrap.cpio', 'disk.img', 'deployment.json'))
    if selected is not None:
        if not complete(selected):
            raise ValueError('selected board fixture is incomplete')
        return selected
    candidates = [p.parent for p in fixtures.glob('*/bootstrap.cpio') if complete(p.parent)]
    if not candidates:
        raise ValueError('no complete board fixture available for stack diagnosis')
    return max(candidates, key=lambda p: (p / 'bootstrap.cpio').stat().st_mtime_ns)


def run(args, report):
    settings = set(args.config.read_text().splitlines())
    if not {'CONFIG_ARCH_AARCH64=y', 'CONFIG_ARM64_VA_BITS=48',
            'CONFIG_KERNEL_STACK_SIZE_KB=16'} <= settings:
        raise ValueError('stack watch requires the AArch64 48-bit, 16 KiB CI configuration')
    fixture = select_fixture(args.fixtures, args.fixture)
    report['fixture'] = str(fixture)
    table = symbols(args.elf)
    endpoint = args.output.resolve() / 'qemu.sock'
    endpoint.unlink(missing_ok=True)
    wrapper = args.output.resolve() / 'qemu-wrapper'
    wrapper.write_text('#!/usr/bin/env python3\nimport os, sys\n'
                       f'os.execv({args.qemu!r}, [{args.qemu!r}, "-snapshot", "-S", '
                       f'"-gdb", {f"unix:{endpoint},server=on,wait=off"!r}, *sys.argv[1:]])\n')
    wrapper.chmod(0o700)
    command = ['python3', '-B', str(ROOT / 'scripts/run-io-vm.py'), '--qemu', str(wrapper),
               '--image', str(args.image), '--initramfs', str(fixture / 'bootstrap.cpio'),
               '--disk', str(fixture / 'disk.img'), '--board', str(fixture / 'deployment.json')]
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    done = threading.Event()
    serial = bytearray()

    def console():
        with (args.output / 'serial.log').open('wb') as log:
            prompts = 0
            while data := child.stdout.read1(65536):
                log.write(data)
                log.flush()
                serial.extend(data)
                if len(serial) > 1048576:
                    done.set()
                    return
                # Replay the relevant bounded workload on the disk snapshot.
                # The final marker distinguishes output from echo.
                count = bytes(serial).count(b'hyper-sh$ ')
                if count > prompts:
                    prompts = count
                    commands = args.command or ['ls /bin', 'ls /bin']
                    line = ((commands[prompts - 1] + '\n').encode() if prompts <= len(commands)
                            else b'echo HYPER_STACK_WATCH_DONE\n')
                    try:
                        child.stdin.write(line)
                        child.stdin.flush()
                    except BrokenPipeError:
                        return
                if re.search(rb'\nHYPER_STACK_WATCH_DONE\r?\n', serial):
                    done.set()
                    return

    reader = threading.Thread(target=console, daemon=True)
    stream = socket.socket(socket.AF_UNIX)
    stream.settimeout(5)
    reader.start()
    deadline = time.monotonic() + args.timeout
    try:
        while not endpoint.exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('QEMU did not expose its GDB socket')
            time.sleep(.05)
        stream.connect(str(endpoint))
        remote = Remote(stream)
        remote.request('qSupported')
        remote.request('?')
        for slot in range(SLOT_COUNT):
            if time.monotonic() >= deadline:
                raise TimeoutError('deadline while installing stack watchpoints')
            remote.watch(ARENA + slot * STRIDE + GUARD, True)
        minimum = WATCH_SIZE + 1

        def finished():
            check_boot_layout(serial)
            return done.is_set()

        for _ in range(100000):
            stop = remote.resume(deadline, finished)
            check_boot_layout(serial)
            if done.is_set() or time.monotonic() >= deadline:
                report['stop_reason'] = 'workload-complete' if done.is_set() else 'deadline'
                break
            thread = re.search(r'thread:([^;]+)', stop)
            watched = re.search(r'watch:([0-9a-fA-F]+)', stop)
            if not thread or not watched:
                raise ValueError(f'unexpected GDB stop: {stop}')
            remote.request('Hg' + thread[1])
            registers = bytes.fromhex(remote.request('g'))
            if len(registers) < 264:
                raise ValueError('short AArch64 register response')
            values = struct.unpack('<33Q', registers[:264])
            fp, sp, pc = values[29], values[31], values[32]
            bounds = stack_bounds(sp)
            address = int(watched[1], 16)
            if bounds and bounds[0] <= address < bounds[1] and sp - bounds[0] < minimum:
                minimum = sp - bounds[0]
                capture = {'remaining': minimum, 'sp': sp, 'pc': pc, 'thread': thread[1],
                           'bottom': bounds[0], 'top': bounds[1],
                           'frames': unwind(remote, fp, sp, bounds[1])}
                report['captures'].append(capture)
                if minimum < args.minimum_remaining:
                    report['stop_reason'] = 'low-reserve'
                    break
            # QEMU stops before the store. Step this CPU past it with only the
            # triggering watchpoint removed; otherwise the same store retraps.
            bottom = ARENA + ((address - ARENA) // STRIDE) * STRIDE + GUARD
            remote.watch(bottom, False)
            remote.request('vCont;s:' + thread[1])
            remote.watch(bottom, True)
        else:
            report['stop_reason'] = 'stop-limit'
    finally:
        stream.close()
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
        reader.join(timeout=3)
        child.stdin.close()
        child.stdout.close()
        endpoint.unlink(missing_ok=True)
        match = re.search(rb'randomized kernel base 0x([0-9a-f]+)', serial)
        if match:
            report['kernel_base'] = int(match[1], 16)
            symbolize(report['captures'], table, report['kernel_base'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', default=shutil.which('qemu-system-aarch64'))
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    fixtures = parser.add_mutually_exclusive_group(required=True)
    fixtures.add_argument('--fixtures', type=Path, help='select the newest complete fixture below this directory')
    fixtures.add_argument('--fixture', type=Path, help='use this exact complete board fixture')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--minimum-remaining', type=int, default=2048)
    parser.add_argument('--command', action='append',
                        help='Shell command to replay after boot; repeat for an ordered workload')
    args = parser.parse_args()
    if not args.qemu or args.timeout <= 0 or not 0 < args.minimum_remaining <= WATCH_SIZE:
        parser.error('QEMU, a positive timeout and reserve within 1..4096 are required')
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'captures': [], 'stop_reason': 'error', 'diagnostic_only': True,
              'image': str(args.image), 'elf': str(args.elf)}
    try:
        run(args, report)
    except (OSError, ValueError, RuntimeError, EOFError, subprocess.SubprocessError) as error:
        report['error'] = str(error)
    finally:
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        with (args.output / 'backtraces.log').open('w') as log:
            log.write(f'Stack diagnostic: {report["stop_reason"]}\n')
            if 'error' in report:
                log.write(report['error'] + '\n')
            for capture in report['captures']:
                log.write(f'\nremaining={capture["remaining"]} sp=0x{capture["sp"]:x} '
                          f'thread={capture["thread"]}\n')
                for frame in [capture.get('instruction', {'pc': capture['pc']}), *capture['frames']]:
                    log.write(json.dumps(frame) + '\n')
        print(f'Stack diagnostic: {report["stop_reason"]}; see {args.output}')
    return 1 if 'error' in report else 0


if __name__ == '__main__':
    raise SystemExit(main())
