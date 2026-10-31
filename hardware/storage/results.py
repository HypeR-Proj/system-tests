#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Summarize physical-board logs or extract a power-cut acknowledgement oracle."""

import argparse
import json
import math
from pathlib import Path
import re
import statistics

PAYLOAD = 1024 ** 3


def records(path):
    data = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', path.read_text(errors='replace')).replace('\r', '')
    for line in data.splitlines(keepends=True):
        if not line.endswith('\n'):
            continue  # A partially received ACK must never certify durability.
        match = re.fullmatch(r'STORAGE,([A-Z0-9_]+),(\d+),([^\n]+)\n', line)
        if match:
            kind, run, value = match.groups()
            yield kind, int(run), value


def oracle(path, run):
    active = False
    ack = 0
    issued = 0
    seen = False
    payload = False
    for kind, identity, value in records(path):
        if kind == 'BEGIN':
            active = identity == run and value == 'durable'
            if active:
                if seen:
                    raise ValueError('ambiguous run ID: more than one durability run')
                seen = True
        if not active or identity != run:
            continue
        if kind == 'PAYLOAD_BYTES':
            payload = int(value) == PAYLOAD
        elif kind == 'ISSUE':
            issued = int(value)
            if issued != ack + 1 or not payload:
                raise ValueError('incomplete or unordered external log')
        elif kind == 'ACK':
            if int(value) != ack + 1 or issued != ack + 1:
                raise ValueError('incomplete or unordered ACK sequence')
            ack += 1
        elif kind in ('END', 'COMPLETE'):
            raise ValueError('writer completed; this is not an interrupted power-cut run')
    if not ack or ack >= PAYLOAD // 1048576:
        raise ValueError('need a partially completed run with at least one complete external ACK')
    return {'run_id': run, 'ack_mib': ack, 'log': str(path),
            'note': 'Use only with an independently confirmed physical power cut and cold recovery.'}


def measurements(path):
    runs = []
    current = None
    for kind, run, value in records(path):
        if kind == 'BEGIN':
            if current is not None:
                raise ValueError('incomplete measurement before next BEGIN')
            current = {'run': run, 'mode': value, 'values': {}}
        elif current is not None:
            if run != current['run']:
                raise ValueError('interleaved run identities')
            current['values'].setdefault(kind, []).append(int(value))
            if kind == 'END':
                runs.append(current)
                current = None
    if current is not None:
        raise ValueError('incomplete measurement log')
    result = []
    for run in runs:
        values = run['values']
        mode = run['mode']
        if mode not in ('write', 'overwrite', 'sync4k'):
            continue
        if values.get('PAYLOAD_BYTES') != [PAYLOAD] or values.get('VERIFIED') != [PAYLOAD]:
            raise ValueError('missing full 1 GiB verification')
        row = {'run_id': run['run'], 'mode': mode, 'payload_bytes': PAYLOAD}
        if mode == 'sync4k':
            latency = sorted(values.get('SYNC4K_NS', []))
            if len(latency) != 128 or min(latency) <= 0:
                raise ValueError('need all 128 positive sync-write samples')
            row.update(iops=128 * 1e9 / sum(latency),
                       latency_us={f'p{p}': latency[math.ceil(p / 100 * len(latency)) - 1] / 1000
                                   for p in (50, 95, 99, 100)})
        else:
            if any(len(values.get(key, [])) != 1 for key in ('TOTAL_NS', 'WRITE_NS', 'SYNC_NS')):
                raise ValueError('missing timing fields')
            total, write, sync = (values[key][0] for key in ('TOTAL_NS', 'WRITE_NS', 'SYNC_NS'))
            if total <= 0 or write < 0 or sync < 0 or total != write + sync:
                raise ValueError('invalid timing interval')
            row.update(total_ms=total / 1e6, write_ms=write / 1e6, sync_ms=sync / 1e6,
                       mib_per_second=1024 * 1e9 / total)
        result.append(row)
    if not result:
        raise ValueError('no complete performance measurements')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    ack = sub.add_parser('oracle')
    ack.add_argument('log', type=Path)
    ack.add_argument('--run-id', type=int, required=True)
    compare = sub.add_parser('compare')
    compare.add_argument('--hyper', type=Path, required=True)
    compare.add_argument('--linux', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'oracle':
            result = oracle(args.log, args.run_id)
        else:
            systems = {'hyper': measurements(args.hyper), 'linux': measurements(args.linux)}
            medians = {}
            for mode in ('write', 'overwrite', 'sync4k'):
                metric = 'iops' if mode == 'sync4k' else 'mib_per_second'
                values = {name: [row[metric] for row in rows if row['mode'] == mode]
                          for name, rows in systems.items()}
                if all(values.values()):
                    medians[mode] = {name: statistics.median(samples) for name, samples in values.items()}
                    medians[mode]['hyper_over_linux'] = medians[mode]['hyper'] / medians[mode]['linux']
            result = {'measurements': systems, 'medians': medians,
                      'limits': ['Different cards/filesystems are deployment comparisons, not isolated hypervisor overhead.',
                                 'Same-boot readback is not a power-loss proof.',
                                 'Record board, card, kernel, filesystem, mount, thermal and frequency metadata separately.']}
        print(json.dumps(result, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f'storage results: {error}\n')


if __name__ == '__main__':
    main()
