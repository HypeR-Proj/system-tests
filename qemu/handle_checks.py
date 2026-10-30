# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Check the handle inspector through the shell's real delegated capabilities."""

import re


def verify_handles(run):
    run('handle --help', rb'--list-kinds')
    kinds = run('handle --list-kinds', rb'guest-mapping')
    for kind in (b'wait-set', b'physical-device', b'guest-memory', b'guest-mailbox',
                 b'guest-notification', b'native-block', b'virtual-serial',
                 b'device-assignment-authority'):
        if kind not in kinds:
            raise AssertionError(f'missing object kind: {kind!r}')
    if b'unrecognized' in kinds or b'unknown' in kinds:
        raise AssertionError(f'incomplete object catalog: {kinds!r}')
    rights = run('handle --list-rights', rb'\nset-attributes\n')
    assert b'\nlock-file\n' in rights
    run('handle --objects --kind process', rb'\n0x[0-9a-f]{16}\s+process\s')
    run('handle --kind 0xffffffff', rb'\(no matching objects\)')
    run('handle --kind physcial-device', rb'unknown kind', failed=True)
    run('handle --all --right wriet', rb'unknown right', failed=True)
    run('handle --handle 0x1000001', rb'error:', failed=True)
    run('handle missing-process', rb'no visible process named', failed=True)
    run('handle 0xffffffffffffffff', rb'not visible or no longer exists', failed=True)
    run('handle --object 0xffffffffffffffff', rb'not visible or no longer exists', failed=True)

    # The shell's directory handle remains live while short-lived tools exit.
    processes = run('ps --name shell', rb'process\s+0x[0-9a-f]{16}.*shell')
    process = re.search(rb'\nprocess\s+(0x[0-9a-f]{16})\s+-\s+shell\s', processes)
    if not process:
        raise AssertionError(f'missing shell process: {processes!r}')
    koid = int(process[1], 16)
    handles = run('handle shell --kind directory --no-headers')
    record = re.search(rb'\n(0x[0-9a-f]{16})\s+(0x[0-9a-f]{16})\s+directory\s+(\S+)', handles)
    if not record or b'HANDLE ' in handles or b'Process ' in handles:
        raise AssertionError(f'invalid handle rows: {handles!r}')
    handle, obj = record[1].decode(), record[2].decode()
    run(f'handle {koid:#x} --handle {handle}', record[1] + rb'\s+' + record[2])
    run(f'handle -p {koid} --handle {handle} -v', rb'granted-rights: 0x[0-9a-f]{16}; flags: 0x[0-9a-f]{8}')
    run('handle shell --handle 0xffffffffffffffff', rb'handle .* is not present', failed=True)
    run(f'handle --object {obj} -v', rb'refs: kernel-service=')
    owners = run(f'handle --all --object {obj} --no-headers')
    if not re.search(f'\n0x{koid:016x} {handle} {obj} '.encode(), owners):
        raise AssertionError(f'reverse lookup omitted shell: {owners!r}')
    writable = run('handle --all --right write --no-headers')
    rows = re.findall(rb'\n0x[0-9a-f]{16} 0x[0-9a-f]{16} 0x[0-9a-f]{16}\s+\S+\s+(\S+)', writable)
    if not rows or any(b'write' not in rights.split(b'|') for rights in rows):
        raise AssertionError(f'granted-right filter failed: {writable!r}')
    # Exercise multiple registry pages and early consumer exit without EPIPE noise.
    run('handle --no-headers > /handle-objects')
    run(f'grep {obj} /handle-objects', record[2])
    run('handle --kind 0xffffffff --no-headers > /handle-empty')
    run('ls --bytes /handle-empty', rb'\s0\s+/handle-empty')
    # Use a durable process: --all may legitimately report a skipped process
    # when this pipeline's grep exits during the weakly consistent scan.
    piped = run('handle shell | grep -q .', timeout=15)
    if b'handle:' in piped:
        raise AssertionError(f'early pipe close produced an error: {piped!r}')
    run('rm /handle-objects /handle-empty')

    # Exact queries opt into details; tabular queries stay stable for scripts.
    objects = run('handle --kind thread --no-headers')
    thread = re.search(rb'\n(0x[0-9a-f]{16})\s+thread\s', objects)
    if not thread:
        raise AssertionError(f'missing thread objects: {objects!r}')
    # Pick a durable scheduler thread (the oldest/last row), not this tool's thread.
    threads = re.findall(rb'\n(0x[0-9a-f]{16})\s+thread\s', objects)
    run(f'handle --object {threads[-1].decode()}', rb'scheduler-tid: 0x[0-9a-f]{16}; role:')
    for kind, expected in [('vmar', rb'permissions: [r-][w-][x-]; maximum:'),
                           ('byte-channel', rb'peer-holder: process 0x[0-9a-f]{16} "terminal-service"; handle 0x[0-9a-f]{16}')]:
        rows = run(f'handle shell --kind {kind} --no-headers')
        # The shell retains the terminal event reader with exactly
        # READ|WAIT|TRANSFER. This excludes duplex stream sessions and a
        # transient READ|WAIT launch receipt. Detail authority belongs to the
        # delegated inspector; the event reader itself needs no INSPECT right.
        candidates = re.findall(rb'\n(0x[0-9a-f]{16})\s+(0x[0-9a-f]{16})\s+' + kind.encode() + rb'\s+(\S+)', rows)
        match = next((row for row in candidates if kind != 'byte-channel' or
                      set(row[2].split(b'|')) == {b'read', b'wait', b'transfer'}), None)
        if not match:
            raise AssertionError(f'missing shell {kind}: {rows!r}')
        run(f'handle shell --handle {match[0].decode()}', expected)
        run(f'handle --object {match[1].decode()}', expected)
    granted = run('handle shell --kind object-inspector --no-headers')
    assert b'inspect-details' in granted
    task_grants = run('handle shell --kind task-inspector --no-headers')
    assert b'inspect-details' not in task_grants
    run('/bin/./handle shell --kind directory', rb'directory')
    run('cp /bin/handle /handle-copy')
    run('chmod 755 /handle-copy')
    run('/handle-copy', rb'object-inspector capability unavailable', failed=True)
    run('rm /handle-copy')
