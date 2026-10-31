#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Measure one opt-in syscall-cost fixture; validate results without timing limits."""

import argparse
from pathlib import Path
import re
import statistics

from hyper_test.command import Shell
from hyper_test.session import Session, native_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--initramfs', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with Session(native_command(args.qemu, args.image, args.initramfs), args.log) as session:
        session.await_text(rb'HypeR session: console ready\n', timeout=180)
        session.await_text(rb'hyper-sh\$ ')
        result = Shell(session).check('/bin/ps', timeout=180)
        if b'SYSCALL-COST PASS\n' not in result:
            raise AssertionError(f'syscall workload did not complete: {result!r}')
        rows = re.findall(rb'^SYSCALL-COST stage=(\S+) repetition=(\d+) '
                          rb'iterations=(\d+) elapsed_ns=(\d+)$', result, re.M)
        if len(rows) != 27:
            raise AssertionError(f'expected 27 measurements, received {len(rows)}')
        for stage in (b'abi-query', b'ready-event', b'channel-roundtrip'):
            samples = [(int(repetition), int(iterations), int(elapsed))
                       for name, repetition, iterations, elapsed in rows if name == stage]
            if ([sample[0] for sample in samples] != list(range(9))
                    or any(sample[1] != 4096 for sample in samples)):
                raise AssertionError(f'incomplete measurement series: {stage!r} {samples!r}')
            elapsed = [sample[2] for sample in samples]
            print(f'{stage.decode()}: median_ns={statistics.median(elapsed):.0f} '
                  f'min_ns={min(elapsed)} max_ns={max(elapsed)} iterations=4096')


if __name__ == '__main__':
    main()
