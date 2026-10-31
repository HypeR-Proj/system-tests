# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Conservative compact-JSON export admission, before starting the owned emulator."""
import os
import re
import subprocess

from trace_artifacts import SECTOR, image

MIB = 1024 * 1024


def export_budget(series, bins, raw_limit):
    # u64 + comma: <=21 bytes/bin. A compact Sample has three u64s plus
    # fixed field names/braces: <128 bytes. Per-series metadata/options and
    # outer report fields fit comfortably in 16 KiB; reserve another 8 MiB
    # for FAT cluster rounding, directory growth and unrelated boot writes.
    if min(series, bins, raw_limit) < 0:
        raise ValueError('negative report capacity')
    return series * (bins * 21 + raw_limit * 128 + 16 * 1024) + 8 * MIB


def parse_free_bytes(output):
    # Force the stable C locale for mtools. Ask the filesystem tool for free
    # space rather than treating the whole partition as available for export.
    matches = re.findall(r'^\s*([0-9][0-9 ,]*) bytes free\s*$', output, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError('cannot determine FAT free space from mdir')
    return int(matches[0].replace(' ', '').replace(',', ''))


def check_export_capacity(artifacts, required):
    # The caller has not started QEMU; validate both GPTs before accessing
    # the selected partition. Never read a live emulator's disk this way.
    with artifacts.disk.open('rb') as stream:
        image.validate_disk(stream, artifacts.board)
    mdir = image.image_tool('mdir', 'mdir', 'mtools')
    result = subprocess.run([mdir, '-i', f'{artifacts.disk}@@{artifacts.offset}', '::/'],
                            check=True, capture_output=True, text=True, timeout=60,
                            stdin=subprocess.DEVNULL, env=dict(os.environ, LC_ALL='C'))
    free = parse_free_bytes(result.stdout)
    partition = next(p for p in artifacts.board.partitions if p.start * SECTOR == artifacts.offset)
    if free > partition.sectors * SECTOR:
        raise ValueError('FAT free space exceeds partition capacity')
    if free < required:
        raise ValueError(f'insufficient FAT export space before measurement: conservative bound '
                         f'{required / MIB:.1f} MiB, free {free / MIB:.1f} MiB; '
                         'prepare a larger disposable fixture with '
                         'SCHED_BENCH_DATA_MIB=512 (or larger), or reduce --cpus/--repeat')
    return dict(required_bytes=required, free_bytes=free, report_encoding='compact JSON')
