#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Manually collect TCP data with the Mac as client and the Pi as server."""

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import platform
import subprocess


def main():
    if any(os.environ.get(key, '').lower() in ('1', 'true') for key in ('CI', 'GITHUB_ACTIONS')):
        raise SystemExit('Hardware measurements are local/manual only.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('server', type=ipaddress.IPv4Address)
    parser.add_argument('--label', choices=('guest', 'raspbian'), default='guest')
    parser.add_argument('--port', type=int, default=5201)
    parser.add_argument('--seconds', type=int, default=15)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--iperf3', default='iperf3')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.seconds <= 60:
        parser.error('port must be 1..65535 and duration 1..60 seconds')
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {
        'started_utc': datetime.now(timezone.utc).isoformat(),
        'client': platform.platform(), 'server': str(args.server),
        'server_label': args.label, 'port': args.port,
        'iperf3': subprocess.run([args.iperf3, '--version'], check=True,
                                 capture_output=True, text=True, timeout=10).stdout,
        'measurements': [],
    }
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    for streams in (1, 4):
        for direction in ('rx', 'tx'):
            name = f'{args.label}-{direction}-p{streams}'
            command = [args.iperf3, '-4', '-c', str(args.server), '-p', str(args.port),
                       '-P', str(streams), '-t', str(args.seconds), '-O', '2', '-i', '0',
                       '--connect-timeout', '5000', '--get-server-output', '-J']
            if direction == 'tx':
                command.append('-R')
            print(f'{name}: {args.seconds} seconds after 2 seconds warm-up', flush=True)
            row = {'name': name, 'command': command}
            try:
                # Stream to files so interruption/timeout cannot discard the
                # child's partial output, including its error diagnostics.
                with (args.output / f'{name}.json').open('w') as stdout, \
                        (args.output / f'{name}.stderr').open('w') as stderr:
                    result = subprocess.run(command, stdout=stdout, stderr=stderr,
                                            timeout=args.seconds + 30)
                row['exit_status'] = result.returncode
                data = json.loads((args.output / f'{name}.json').read_text())
                if result.returncode or data.get('error'):
                    raise ValueError(data.get('error') or
                                     (args.output / f'{name}.stderr').read_text() or 'iperf3 failed')
                end = data['end']
                row['receiver_mbit_s'] = end['sum_received']['bits_per_second'] / 1e6
                row['sender_retransmits'] = end['sum_sent'].get('retransmits')
                print(f"  receiver: {row['receiver_mbit_s']:.2f} Mbit/s; "
                      f"sender retransmits: {row['sender_retransmits']}", flush=True)
            except subprocess.TimeoutExpired:
                row['error'] = 'iperf3 exceeded the bounded test timeout'
            except KeyboardInterrupt:
                row['error'] = 'collection interrupted'
            except (ValueError, KeyError, TypeError, OSError) as error:
                row['error'] = str(error)
            summary['measurements'].append(row)
            (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
            if 'error' in row:
                raise SystemExit(f"{name}: {row['error']}; partial evidence saved in {args.output}")
    print(f'Results: {args.output / "summary.json"}')


if __name__ == '__main__':
    main()
