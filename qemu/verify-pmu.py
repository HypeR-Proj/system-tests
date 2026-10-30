#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Exercise physical PMUv3 overflow, bounded capture, restart and symbolization."""
import argparse
import json
from pathlib import Path
import re
import sys
import time

from hyper_test.command import Shell
from hyper_test.guest_console import append_console_output
from hyper_test.session import Session, native_command

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/profiling'))
from flamegraph import Symbols, captures, svg


def await_disarmed(shell):
    # Wait for the autonomous deadline path, without issuing a stop syscall.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = shell.check('perf status')
        if b'running=false armed=0x0' in status:
            return
        shell.session.pump(0.1)
    raise AssertionError(f'capture did not automatically disarm: {status!r}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('qemu')
    parser.add_argument('image')
    parser.add_argument('initramfs')
    parser.add_argument('logfile', type=Path)
    parser.add_argument('--kernel', type=Path, required=True)
    parser.add_argument('--nm', required=True)
    parser.add_argument('--unsupported', action='store_true')
    args = parser.parse_args()
    with Session(native_command(args.qemu, args.image, args.initramfs), args.logfile,
                 output_filter=append_console_output) as session:
        session.await_text(rb'HypeR session: console ready\n', timeout=180)
        session.await_text(rb'hyper-sh\$ ')
        shell = Shell(session)
        if args.unsupported:
            result = shell.run('perf status')
            result.require('perf status', failed=True)
            if b'not supported' not in result.output.lower() and b'NotSupported' not in result.output and b'Status(-4)' not in result.output:
                raise AssertionError(f'expected unsupported PMU: {result.output!r}')
            return
        initial = shell.check('perf status')
        if b'running=false armed=0x0' not in initial:
            raise AssertionError(f'PMU armed before a capture was requested: {initial!r}')
        # Start/stop control must work promptly even at the slowest sampling
        # period, without waiting for a PMU overflow to reconcile the request.
        for _ in range(3):
            shell.check('perf start --seconds 1 --period 1000000000')
            shell.check('perf stop')
            status = shell.check('perf status')
            if b'running=false armed=0x0' not in status:
                raise AssertionError(f'explicit stop left a CPU armed: {status!r}')
        # Cross the cancelled capture's original deadline while its successor
        # remains active. A late timer callback must not close the successor.
        shell.check('perf start --seconds 30 --period 1000000000')
        session.pump(1.2)
        status = shell.check('perf status')
        cpus = re.search(rb'CPUs=(\d+)', status)
        if cpus is None:
            raise AssertionError(f'missing CPU count: {status!r}')
        mask = (1 << int(cpus.group(1))) - 1
        if f'running=true armed=0x{mask:x}'.encode() not in status:
            raise AssertionError(f'IPI start did not arm every CPU: {status!r}')
        shell.check('perf stop')
        shell.check('perf start --seconds 1 --period 1000000000')
        await_disarmed(shell)
        shell.check('perf start --seconds 5 --period 1000000')
        shell.run('perf start').require('second capture must be busy', failed=True)
        shell.run('perf start --period 1').require('unbounded sample rate must be rejected', failed=True)
        session.pump(2)
        shell.check('perf dump --output /tmp/pmu-wrap.jsonl')
        wrapped = shell.check('cat /tmp/pmu-wrap.jsonl')
        rows = [line for line in wrapped.decode().splitlines() if line.startswith('{')]
        _, metadata = captures(rows, lambda offset: f'0x{offset:x}')
        if metadata['overwritten'] == 0:
            raise AssertionError('bounded ring overwrite was not exercised')
        first_generation = json.loads(rows[0])['generation']
        shell.check('perf record --seconds 3 --period 100000000 --output /tmp/pmu.jsonl')
        output = shell.check('cat /tmp/pmu.jsonl')
        lines = [line for line in output.decode().splitlines() if line.startswith('{')]
        header = json.loads(lines[0])
        if header['generation'] <= first_generation:
            raise AssertionError('new capture reused an old generation')
        symbols = Symbols(args.kernel, args.nm, header['kernel'])
        stacks, metadata = captures(lines, symbols.resolve)
        rows = [json.loads(line) for line in lines[1:-1]]
        if {row['cpu'] for row in rows} != set(range(header['cpus'])):
            raise AssertionError('some CPUs never delivered a real PMU overflow')
        if not any(len(row['frames']) > 1 for row in rows):
            raise AssertionError('no kernel stack was unwound')
        if not any('hyper::' in stack for stack in stacks):
            raise AssertionError('kernel function names were not resolved')
        args.logfile.with_suffix('.jsonl').write_text('\n'.join(lines) + '\n')
        args.logfile.with_suffix('.svg').write_text(svg(stacks, metadata))
        args.logfile.with_suffix('.folded').write_text(''.join(f'{s} {n}\n' for s, n in sorted(stacks.items())))
        status = shell.check('perf status')
        if b'running=false armed=0x0' not in status:
            raise AssertionError(f'capture did not automatically disarm: {status!r}')
        # Once disarmed, ordinary timer/UART/device IRQs and Native activity
        # must leave every retained sample unchanged, including late overflow.
        shell.check('perf dump --output /tmp/stopped-before.jsonl')
        frozen = shell.check('cat /tmp/stopped-before.jsonl')
        for command in ('ps', 'free', 'echo PMU_STOPPED_IRQ_ACTIVITY'):
            shell.check(command)
        session.pump(1)
        shell.check('perf dump --output /tmp/stopped-after.jsonl')
        later = shell.check('cat /tmp/stopped-after.jsonl')
        before_rows = [json.loads(line) for line in frozen.splitlines() if line.startswith(b'{')]
        after_rows = [json.loads(line) for line in later.splitlines() if line.startswith(b'{')]
        if not before_rows or before_rows != after_rows:
            raise AssertionError('ordinary IRQ activity changed a stopped capture')
        shell.check('perf record --seconds 3 --output /tmp/command.jsonl -- /bin/echo PMU_COMMAND_OK')
        shell.run('perf record --seconds 3 --output /tmp/failed.jsonl -- /bin/cat /missing-pmu-input').require('failed workload', failed=True)
        shell.run('perf record --seconds 3 --output /missing-directory/pmu.jsonl -- /bin/echo discarded').require('failed export', failed=True)
        shell.check('perf stop')
        # A copy outside the shell's trusted tool path receives no PMU authority.
        shell.check('cp /bin/perf /tmp/unprivileged-perf')
        shell.run('/tmp/unprivileged-perf status').require('unprivileged PMU read', failed=True)
    print('PMUv3 overflow/capture/flamegraph acceptance passed')


if __name__ == '__main__':
    main()
