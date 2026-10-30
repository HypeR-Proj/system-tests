#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Prove remote filesystem RPC correlation beyond userspace IPC wrappers."""

import argparse
import json
from pathlib import Path

from hyper_test.command import Shell
from hyper_test.guest_console import append_console_output
from hyper_test.session import Session
from trace_artifacts import TraceArtifacts, boot_command
from trace_validation import validate_remote, validate_block


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('qemu', 'image', 'initramfs', 'disk', 'board', 'log'):
        parser.add_argument('--' + argument, required=True)
    args = parser.parse_args()
    log = Path(args.log)
    artifacts = TraceArtifacts(args.disk, args.board, log.with_suffix('.artifacts'))
    with Session(boot_command(args.qemu, args.image, args.initramfs, args.disk, args.board),
                 log, output_filter=append_console_output) as session:
        session.await_text(rb'HypeR: startup complete\n', timeout=240)
        session.await_text(rb'hyper-sh\$ ')
        shell = Shell(session)
        # Bound the snapshot to the FAT mutation, without live readers contending
        # with ring writers. Warm page-in and omit unrelated scheduler traffic.
        shell.check('cp /etc/hyper/components.json /tmp/trace-warmup')
        capture_result = shell.run('perf trace record --raw-events --format chrome --snapshot --duration-ms 30000 --categories ipc,io --no-names --poll-ms 1 --fail-on-loss --max-events 32768 -o /tmp/remote.json -- /bin/cp /etc/hyper/components.json /data/trace-created', timeout=180)
        if capture_result.status == 0:
            shell.check('perf trace clear')
        TraceArtifacts.save(shell, ('remote.json',))
    files = artifacts.extract(session, ('remote.json',))
    capture_result.require('filesystem capture')
    payload = json.loads(files['remote.json'].read_bytes())
    validate_remote(payload)
    validate_block(payload)
    log.with_suffix('.json').write_bytes(files['remote.json'].read_bytes())
    print('Remote filesystem RPC, asynchronous block lifetimes and causal export passed')


if __name__ == '__main__':
    main()
