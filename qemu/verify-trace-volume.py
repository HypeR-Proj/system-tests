#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Export the same large real capture through FAT and RAMFS."""

import argparse
from pathlib import Path
import sys

from hyper_test.command import Shell
from hyper_test.session import Session
from trace_artifacts import TraceArtifacts, boot_command

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'profiling'))
from perfetto_proto import inspect as inspect_proto, one


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('qemu')
    parser.add_argument('image')
    parser.add_argument('initramfs')
    parser.add_argument('logfile', type=Path)
    parser.add_argument('--disk', type=Path, required=True)
    parser.add_argument('--board', type=Path, required=True)
    args = parser.parse_args()
    artifacts = TraceArtifacts(args.disk, args.board, args.logfile.with_suffix('.artifacts'))
    with Session(boot_command(args.qemu, args.image, args.initramfs, args.disk, args.board), args.logfile) as session:
        session.await_text(rb'hyper-sh\$ ', timeout=240)
        shell = Shell(session)
        result = shell.run('perf trace record --snapshot --raw-events --duration-ms 120000 --categories ipc --buffer-records 65536 --max-events 262144 --fail-on-loss -o /data/trace-artifact-large.pftrace -- /bin/lifecycle-stress trace-volume', timeout=240)
        # Repeat export from the same frozen capture through RAMFS. Both paths
        # must survive the hardware's old 32 MiB boundary without postprocessing.
        ram_result = shell.run('perf trace dump --raw-events --fail-on-loss -o /tmp/large-ram.pftrace', timeout=240)
        shell.check('cp /tmp/large-ram.pftrace /data/trace-artifact-large-ram.pftrace', timeout=240)
        shell.check('umount /data', timeout=120)
    files = artifacts.extract(session, ('large.pftrace', 'large-ram.pftrace'))
    result.require('large direct trace export')
    ram_result.require('large RAMFS trace export')
    path = files['large.pftrace']
    if path.stat().st_size <= 32 * 1024 * 1024:
        raise AssertionError('fixture did not cross the hardware export failure boundary')
    trace = inspect_proto(path.read_bytes())
    ram_trace = inspect_proto(files['large-ram.pftrace'].read_bytes())
    if trace['export_complete'] is not True or ram_trace['export_complete'] is not True:
        raise AssertionError('large export lacks its serialization completion receipt')
    if (files['large-ram.pftrace'].stat().st_size <= 32 * 1024 * 1024
            or trace['metadata']['collected_events'] != ram_trace['metadata']['collected_events']
            or trace['metadata']['generation'] != ram_trace['metadata']['generation']):
        raise AssertionError('large RAMFS export did not retain the same complete capture')
    metadata = trace['metadata']
    if not metadata['cross_cpu_spans'] or metadata['limited']:
        raise AssertionError('large capture lost source observations')
    # Two nested client RPCs per request. Additional filesystem RPCs are allowed.
    completed = sum(one(event, 23) == b'RPC round trip' for _, event in trace['events'])
    if completed < 8192:
        raise AssertionError(f'large export lost RPC spans: {completed}')
    print(f'Large trace export passed: FAT={path.stat().st_size} bytes, '
          f'RAMFS={files["large-ram.pftrace"].stat().st_size} bytes, {completed} RPC spans')


if __name__ == '__main__':
    main()
