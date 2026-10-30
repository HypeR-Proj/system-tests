#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Run the dedicated Native stack workload and inspect retired-stack evidence.

Audit builds measure modified-byte watermarks, not complete stack-pointer bounds.
Use --no-audit builds for timing comparisons; audit scans perturb performance.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

spec = importlib.util.spec_from_file_location(
    'guest_console', Path(__file__).with_name('verify-guest-smp.py'))
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


def audit_summary(data, minimum_remaining, required_kinds=frozenset({'kernel', 'user', 'irq', 'vcpu'}),
                  maximum_used=None):
    records = re.findall(rb'HypeR STACK-AUDIT ([^\r\n]+)\r?\n', data)
    summary = {}
    for record in records:
        fields = dict(re.findall(rb'(\w+)=([^ ]+)', record))
        try:
            kind = fields[b'kind'].decode('ascii')
            used, remaining, size = (int(fields[key]) for key in
                                     (b'used', b'remaining', b'size'))
            canary = fields[b'canary']
        except (KeyError, ValueError, UnicodeError) as error:
            raise ValueError('malformed stack audit record') from error
        if canary != b'true':
            raise ValueError('stack canary damaged')
        # Eight bytes at the mapping bottom are reserved for the canary.
        if min(used, remaining) < 0 or used + remaining + 8 != size:
            raise ValueError('inconsistent stack accounting')
        if remaining < minimum_remaining:
            raise ValueError(f'{kind} stack reserve {remaining} < {minimum_remaining}')
        if maximum_used is not None and used > maximum_used:
            raise ValueError(f'{kind} stack usage {used} > {maximum_used}')
        summary[kind] = max(summary.get(kind, 0), used)
    if not required_kinds <= summary.keys():
        raise ValueError('missing retired kernel/user/vCPU or local IRQ stack observation')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('qemu', 'image', 'initramfs', 'log'):
        parser.add_argument(name)
    parser.add_argument('--no-audit', action='store_true')
    parser.add_argument('--minimum-remaining', type=int, default=0)
    parser.add_argument('--maximum-used', type=int)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--stage', choices=('all', 'inspect', 'memory', 'process'), default='all')
    args = parser.parse_args()
    if (args.minimum_remaining < 0 or args.repetitions < 1
            or (args.maximum_used is not None and args.maximum_used < 1)):
        parser.error('reserve must be nonnegative; repetitions and maximum usage must be positive')
    command = [args.qemu, '-machine', os.environ.get('QEMU_MACHINE',
               'virt,virtualization=on,gic-version=3,dtb-randomness=on'),
               '-cpu', os.environ.get('QEMU_CPU', 'max'),
               '-smp', os.environ.get('QEMU_CPUS', '4'),
               '-m', os.environ.get('QEMU_MEMORY', '1G'),
               '-nodefaults', '-display', 'none', '-serial', 'stdio',
               '-no-reboot', '-monitor', 'none', '-kernel', args.image,
               '-initrd', args.initramfs, '-append', os.environ.get(
                   'QEMU_BOOTARGS', 'earlycon=pl011,mmio32,0x09000000')]
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    with open(args.log, 'wb') as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        selector = selectors.DefaultSelector()
        raw, pending = bytearray(), bytearray()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)

            def await_text(pattern, timeout=90, raw_since=None):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    source = pending if raw_since is None else raw[raw_since:]
                    match = re.search(pattern, source)
                    if match:
                        result = match.group(0)
                        if raw_since is None:
                            del pending[:match.end()]
                        return result
                    if process.poll() is not None:
                        raise RuntimeError(f'QEMU exited {process.returncode} before {pattern!r}')
                    for key, _ in selector.select(0.2):
                        data = os.read(key.fd, 65536)
                        log.write(data)
                        log.flush()
                        raw.extend(data)
                        console.append_console_output(pending, data)
                raise TimeoutError(f'waiting for {pattern!r}; see {args.log}')

            def send(text):
                process.stdin.write(text + b'\n')
                process.stdin.flush()

            await_text(rb'HypeR session: console ready')
            await_text(rb'HypeR: vCPU 0 running as scheduler thread', raw_since=0)
            send(b'echo HYPER_STACK_READY')
            await_text(rb'(?:^|\n)HYPER_STACK_READY\n')
            await_text(rb'hyper-sh\$ ')
            send(b'vmm console alpine')
            await_text(rb'Connected to alpine\.')
            await_text(rb'~ # ')
            process.stdin.write(b'\x1d')
            process.stdin.flush()
            await_text(rb'\[vmm\] d/q/Ctrl-\]: detach')
            process.stdin.write(b'd')
            process.stdin.flush()
            await_text(rb'\[vmm\] detached')
            await_text(rb'hyper-sh\$ ')
            for iteration in range(args.repetitions):
                send(b'/bin/ps --stack-workload ' + args.stage.encode('ascii'))
                for stage in (b'inspect', b'memory', b'process'):
                    if args.stage != 'all' and stage.decode('ascii') != args.stage:
                        continue
                    result = await_text(rb'HYPER_STACK_WORKLOAD stage=' + stage
                                        + rb' [^\n]*elapsed_ns=\d+ success=1\n')
                    print(f'iteration={iteration + 1} ' + result.decode().strip())
                await_text(rb'(?:^|\n)HYPER_STACK_WORKLOAD_OK\n')
                await_text(rb'hyper-sh\$ ')
            stop_start = len(raw)
            send(b'vmm stop alpine')
            await_text(rb'hyper-sh\$ ')
            deadline = time.monotonic() + 60
            while (remaining := deadline - time.monotonic()) > 0:
                send(b'vmm status alpine')
                status = await_text(rb'alpine\s+(?:starting|running|stopping|stopped|failed)',
                                    timeout=remaining)
                await_text(rb'hyper-sh\$ ', timeout=max(0.01, deadline - time.monotonic()))
                if status.endswith(b'stopped'):
                    break
                if status.endswith(b'failed'):
                    raise RuntimeError('VM failed during stack workload shutdown')
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            else:
                raise TimeoutError('VM did not stop after stack workload')
            # A post-stop command proves the control command returned, without
            # confusing a preexisting prompt with current command completion.
            send(b'echo HYPER_STACK_STOPPED')
            await_text(rb'(?:^|\n)HYPER_STACK_STOPPED\n')
            await_text(rb'hyper-sh\$ ')
            if not args.no_audit:
                await_text(rb'HypeR STACK-AUDIT kind=vcpu [^\n]*\n', raw_since=stop_start)
                print('stack watermark maxima:', audit_summary(
                    raw, args.minimum_remaining, maximum_used=args.maximum_used))
            print('HYPER_STACK_TEST_OK')
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()


if __name__ == '__main__':
    main()
