#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Validate privileged static trace capture and actual exported event payloads."""
import argparse
from pathlib import Path
import time

from hyper_test.command import Shell
from hyper_test.guest_console import append_console_output
from hyper_test.session import Session
from trace_artifacts import TraceArtifacts, boot_command
from trace_validation import validate_capture


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
    names = ('frozen.jsonl', 'later.jsonl', 'trace.json', 'ipc.json', 'command.jsonl', 'remote.json', 'failed.json', 'expiry.json', 'lanes.pftrace', 'native.pftrace')
    with Session(boot_command(args.qemu, args.image, args.initramfs, args.disk, args.board), args.logfile,
                 output_filter=append_console_output) as session:
        session.await_text(rb'HypeR: startup complete\n', timeout=240)
        session.await_text(rb'hyper-sh\$ ')
        shell = Shell(session)
        if b'running=false' not in shell.check('perf trace status'):
            raise AssertionError('tracing enabled at boot')
        shell.check('cp /bin/perf /tmp/unprivileged-perf')
        shell.run('/tmp/unprivileged-perf trace status').require('unprivileged trace', failed=True)
        shell.run('perf trace start --cpus 255').require('offline CPU', failed=True)
        shell.check('perf trace start --duration-ms 1000 --categories scheduler')
        shell.check('perf trace stop')
        if b'retained=16384/CPU maximum=16384/CPU' not in shell.check('perf trace status'):
            raise AssertionError('default trace allocation did not grow to 16384 records')
        shell.run('perf trace clear --generation 0').require('stale clear generation', failed=True)
        shell.check('perf trace clear')
        cleared = shell.check('perf trace status')
        if b'generation=0 ' not in cleared or b'retained=0/CPU' not in cleared:
            raise AssertionError('clear retained capture metadata')
        shell.run('perf trace dump -o /tmp/cleared.pftrace').require('cleared capture', failed=True)
        shell.check('perf trace start --duration-ms 60000 --categories scheduler')
        session.pump(1.2)
        if b'running=true' not in shell.check('perf trace status'):
            raise AssertionError('cancelled deadline stopped the replacement capture')
        # Check exclusion while a long-lived capture is active. A separate
        # command can take longer than the short expiry test on a TCG runner.
        shell.run('perf trace start').require('concurrent capture', failed=True)
        shell.run('perf trace clear').require('clear active capture', failed=True)
        shell.check('perf trace stop')
        shell.check('perf trace clear')
        # Use a tiny ring and CPU filter: this intentionally exercises overwrite.
        shell.check('perf trace start --duration-ms 1200 --buffer-records 64 --cpus 0 --categories scheduler,syscall,irq')
        deadline = time.monotonic() + 20
        while b'running=false' not in shell.check('perf trace status'):
            if time.monotonic() > deadline:
                raise AssertionError('trace did not stop autonomously')
            session.pump(0.1)
        shell.check('perf trace dump --raw-events --format jsonl --no-names -o /tmp/frozen.jsonl')
        shell.run('perf trace dump --raw-events --format chrome --generation 0 -o /tmp/stale.json').require('stale generation', failed=True)
        shell.run('perf trace dump --raw-events --format chrome --fail-on-loss -o /tmp/loss.json').require('loss policy', failed=True)
        shell.check('echo TRACE_STOPPED; ps; free')
        shell.check('perf trace dump --raw-events --format jsonl --no-names -o /tmp/later.jsonl')
        shell.check('perf trace clear')
        shell.check('perf trace record --raw-events --format chrome --duration-ms 1000 --categories scheduler,syscall,ipc --max-events 128 -o /tmp/trace.json')
        shell.run('perf trace dump --raw-events --format chrome -o /tmp/trace.json').require('overwrite requires opt-in', failed=True)
        shell.check('perf trace clear')
        # Bounded lossless workload: two hops, duplex queues, explicit parents,
        # real blocked waits, and repeated protocol IDs on distinct connections.
        # One bounded command excludes serial control traffic and keeps the
        # collector off live rings. Continuous recording is tested separately.
        ipc_result = shell.run('perf trace record --raw-events --format chrome --snapshot --duration-ms 30000 --categories ipc,scheduler,irq --poll-ms 1 --no-names --fail-on-loss --max-events 16384 -o /tmp/ipc.json -- /bin/lifecycle-stress trace', timeout=120)
        if ipc_result.status == 0:
            shell.check('perf trace clear')
        # This checks exit semantics, not command latency. Leave room for slow
        # TCG runners; command completion still stops the recording immediately.
        # Live reads can contend with the terminal syscall's nonblocking writer.
        # Require a complete snapshot before asserting that its exit was recorded.
        command_result = shell.run('perf trace record --raw-events --snapshot --no-names --fail-on-loss --duration-ms 30000 --categories syscall --max-events 16384 --format jsonl -o /tmp/command.jsonl -- /bin/echo TRACE_COMMAND_OK', timeout=120)
        if command_result.status == 0:
            shell.check('perf trace clear')
        shell.run('perf trace record --raw-events --format chrome --duration-ms 1000 --max-events 32 -o /tmp/failed.json -- /bin/cat /missing-trace-input').require('workload failure', failed=True)
        if b'running=false' not in shell.check('perf trace status'):
            raise AssertionError('collector left recording enabled')
        # Export happens outside all measured intervals. Serial carries command
        # status only; no trace body is printed or hex-framed by test-report.
        # Warm executable/source pages before this bounded FAT mutation. Live
        # readers can make nonblocking ring writers drop events on another CPU.
        # Snapshot only the RPC/block categories needed here; scheduler coverage
        # belongs to the separate IPC workload above, keeping the snapshot bounded
        # without weakening fail-on-loss validation.
        shell.check('cp /etc/hyper/components.json /tmp/trace-warmup')
        remote_result = shell.run('perf trace record --raw-events --format chrome --snapshot --duration-ms 30000 --categories ipc,io --no-names --poll-ms 1 --fail-on-loss --max-events 32768 -o /tmp/remote.json -- /bin/cp /etc/hyper/components.json /data/trace-created', timeout=180)
        if remote_result.status == 0:
            shell.check('perf trace clear')
        # Deadline-only snapshots test autonomous stop, not maximum syscall
        # production rate; sparse scheduler observations keep the probe bounded.
        shell.check('perf trace record --raw-events --format chrome --snapshot --duration-ms 50 --categories scheduler --no-names --fail-on-loss -o /tmp/expiry.json')
        shell.check('perf trace clear')
        shell.check('perf trace record --duration-ms 250 --categories scheduler --max-events 16384 -o /tmp/lanes.pftrace')
        shell.check('perf trace clear')
        native_result = shell.run('perf trace record --snapshot --duration-ms 30000 --categories ipc,scheduler --poll-ms 1 --fail-on-loss --max-events 16384 -o /tmp/native.pftrace -- /bin/lifecycle-stress trace', timeout=120)
        if native_result.status == 0:
            shell.check('perf trace clear')
        TraceArtifacts.save(shell, names)
    files = artifacts.extract(session, names)
    # Preserve all raw files before propagating strict capture failures.
    ipc_result.require('bounded IPC snapshot')
    command_result.require('bounded command exit snapshot')
    remote_result.require('filesystem capture')
    native_result.require('native Perfetto IPC snapshot')
    validate_capture(files, args.logfile)
    print('Trace lifecycle, CPU filtering, loss and Perfetto export acceptance passed')


if __name__ == '__main__':
    main()
