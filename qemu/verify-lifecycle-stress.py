#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Bounded Native lifecycle stress with seeds and structured command evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from hyper_test.command import Shell
from hyper_test.session import Session, native_command


def validate(output, seed, rounds):
    begins = re.findall(rb'(?m)^STRESS_BEGIN round=(\d+) seed=(\d+) normal_exit=(true|false)$', output)
    expected = [(str(index).encode(), str((seed + index) % 2**64).encode(),
                 b'true' if (seed + index) % 2**64 % 3 == 2 else b'false')
                for index in range(rounds)]
    passes = re.findall(rb'(?m)^STRESS_PASS round=(\d+) transfers=(\d+)$', output)
    if (begins != expected or [int(index) for index, _ in passes] != list(range(rounds))
            or any(int(count) == 0 for _, count in passes)
            or output.count(f'LIFECYCLE_STRESS_OK seed={seed} rounds={rounds}\n'.encode()) != 1):
        raise AssertionError(f'incomplete lifecycle stress: {output!r}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('qemu', 'image', 'initramfs', 'log'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--seed', type=int, default=175)
    parser.add_argument('--rounds', type=int, default=12)
    args = parser.parse_args()
    if not 0 <= args.seed < 2**64 or not 1 <= args.rounds <= 10000:
        parser.error('seed must be u64; rounds must be 1..10000')
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    command = native_command(args.qemu, args.image, args.initramfs)
    command.extend(['-accel', 'tcg,thread=multi'])
    log.with_suffix('.json').write_text(json.dumps({
        'seed': args.seed, 'rounds': args.rounds, 'qemu': command,
        'cpus': os.environ.get('QEMU_CPUS', '4'),
        'sha256': {name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                   for name, path in (('kernel', args.image), ('initramfs', args.initramfs))},
    }, indent=2) + '\n')
    with Session(command, log) as vm:
        boot = vm.await_text(rb'hyper-sh\$ |fallback-sh\$ |HypeR: startup incomplete', timeout=180)
        if not boot.endswith(b'hyper-sh$ '):
            raise AssertionError(f'Native startup failed: {boot[-4096:]!r}')
        shell = Shell(vm)
        # Small batches bound capture size and provide progress on long manual
        # runs. Never rerun a failing workload to manufacture successful evidence.
        for start in range(0, args.rounds, 12):
            count = min(12, args.rounds - start)
            seed = (args.seed + start) % 2**64
            print(f'lifecycle stress: seed={seed} rounds={count}', flush=True)
            try:
                output = shell.check(f'/bin/lifecycle-stress {seed} {count}', timeout=240)
                validate(output, seed, count)
            except Exception:
                # Preserve the first failure even if diagnostics are unusable.
                for diagnostic in ('ps --threads', 'free', 'journalctl -n 100'):
                    try:
                        shell.check(diagnostic, timeout=10)
                    except Exception:
                        break
                raise
        shell.check('echo STRESS_SHELL_ALIVE')
    print(f'lifecycle stress passed: {log}')


if __name__ == '__main__':
    main()
