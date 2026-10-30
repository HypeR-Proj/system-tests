# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Inspect the small public Perfetto protobuf subset without extra dependencies.

This validates artifact contents in CI. Import/analysis semantics are checked
separately with the official Trace Processor by verify-perfetto.py.
"""

import json


def fields(data):
    position = 0

    def varint():
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position == len(data):
                raise ValueError('truncated protobuf varint')
            byte = data[position]
            position += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('oversized protobuf varint')

    result = {}
    while position < len(data):
        key = varint()
        number, kind = key >> 3, key & 7
        if not number:
            raise ValueError('zero protobuf field')
        if kind == 0:
            value = varint()
        elif kind in (1, 2, 5):
            size = varint() if kind == 2 else (8 if kind == 1 else 4)
            end = position + size
            if end > len(data):
                raise ValueError('truncated protobuf field')
            value = data[position:end]
            position = end
            if kind != 2:
                value = int.from_bytes(value, 'little')
        else:
            raise ValueError(f'unsupported protobuf wire type {kind}')
        result.setdefault(number, []).append(value)
    return result


def one(message, field, default=None):
    values = message.get(field, [])
    if len(values) > 1:
        raise ValueError(f'duplicate singular field {field}')
    return values[0] if values else default


def inspect(data):
    packets = [fields(packet) for packet in fields(data).get(1, [])]
    timestamps = [one(packet, 8) for packet in packets]
    if not timestamps or timestamps != sorted(timestamps):
        raise ValueError('empty or unordered trace packets')
    states, trees, events, descriptors = [], [], [], []
    for packet in packets:
        for number, target in ((117, states), (122, trees), (11, events), (60, descriptors)):
            if number in packet:
                target.append((one(packet, 8), fields(one(packet, number))))
    metadata = [json.loads(one(d, 14).decode()) for _, d in descriptors
                if one(d, 1) == 1 and one(d, 14)]
    if len(metadata) != 1:
        raise ValueError('missing capture identity')
    threads = [fields(t) for _, tree in trees for t in tree.get(2, [])]
    tids = {one(t, 1) for t in threads}
    for identity, observation in metadata[0].get('thread_objects', {}).items():
        tid = metadata[0].get('exported_threads', {}).get(identity)
        if tid is not None and tid != int(observation['koid'], 16):
            raise ValueError(f'thread TID does not match its KOID: {identity}: {observation}')
    processes = [fields(p) for _, tree in trees for p in tree.get(1, [])]
    pids = {one(p, 1) for p in processes}
    if tids & pids:
        raise ValueError('process/main-thread IDs collide with exported scheduler TIDs')
    if len(tids) != len(threads) or any(one(t, 2) not in pids for t in threads):
        raise ValueError('duplicate thread or missing process owner')
    process_names = {one(p, 1): one(p, 3).decode() for p in processes}
    thread_owners = {one(t, 1): process_names[one(t, 2)] for t in threads}
    for identity, vm in metadata[0].get('vm_threads', {}).items():
        tid = metadata[0].get('exported_threads', {}).get(identity)
        if tid is not None and thread_owners.get(tid) != f'vm-{vm["name"]}':
            raise ValueError(f'incorrect vCPU owner: {identity}: {vm}')
    if any(one(s, 3) not in tids or one(s, 4) not in range(2, 9) for _, s in states):
        raise ValueError('invalid scheduler identity/state')
    return dict(packets=packets, states=states, threads=threads, trees=trees,
                events=events, descriptors=descriptors, metadata=metadata[0])
