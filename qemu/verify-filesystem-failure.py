#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Prove remote filesystem death retires cache hits and pending I/O."""
import argparse
from pathlib import Path

from hyper_test.session import Session, native_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    for name in ('image', 'initramfs', 'log'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    failures = (b'FS-FAILURE: FAIL', b'HypeR: fatal', b'HypeR KERNEL PANIC',
                b'kernel panic', b'HypeR crash monitor', b'panicked at')
    with Session(native_command(args.qemu, args.image, args.initramfs),
                 args.log, failures=failures) as session:
        session.await_text(rb'FS-FAILURE: delayed-reply PASS\n', timeout=120)
        session.await_text(rb'FS-FAILURE: /fs-worker-stop PASS\n', timeout=120)
        session.await_text(rb'FS-FAILURE: /fs-owner-loss PASS\n')
        session.await_text(rb'FS-FAILURE: pending-stop PASS\n')
        session.await_text(rb'FS-FAILURE: caller-stop PASS\n')
        session.await_text(rb'FS-FAILURE: write-overflow PASS\n')
        session.await_text(rb'FS-FAILURE: protected-file-cache PASS\n')
        session.await_text(rb'FS-FAILURE: protection-exit PASS\n')
        session.await_text(rb'FS-FAILURE: directory-cache PASS\n')
        session.await_text(rb'FS-FAILURE: PASS\n')
    print(f'Filesystem failure isolation passed: {args.log}')


if __name__ == '__main__':
    main()
