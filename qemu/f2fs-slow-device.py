#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
"""Run the F2FS streaming boot with a slow physical backing disk."""
import os
import sys

RATE = 4 * 1024 * 1024
args = sys.argv[1:]
selected = 0
for index in range(len(args) - 1):
    if args[index] == '-drive' and args[index + 1].startswith('if=none,id=physicaldisk,'):
        args[index + 1] += f',bps_wr={RATE}'
        selected += 1
if selected != 1:
    raise SystemExit('F2FS slow-device test requires exactly one physical backing drive')
qemu = os.environ['HYPER_F2FS_TEST_QEMU']
print(f'F2FS slow-device write rate: {RATE} bytes/s', file=sys.stderr, flush=True)
os.execvp(qemu, [qemu, *args])
