# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Validate complete raw trace artifacts on the host after the disk is fenced."""

import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'profiling'))
from perfetto_proto import inspect as inspect_proto, one


def complete(payload):
    metadata = payload['metadata']
    return (metadata['cross_cpu_spans'] and not metadata['limited']
            and not any(row['overwritten'] or row['incomplete'] for row in metadata['losses']))


def validate_remote(payload):
    if not complete(payload):
        raise AssertionError('remote RPC capture is incomplete')
    events = [event for event in payload['traceEvents'] if event['ph'] == 'i']
    clients = [e for e in events if e['name'] == 'rpc_client_begin']
    servers = {(e['args']['connection'], e['args']['request']) for e in events
               if e['name'] == 'rpc_server_begin'}
    # Direct kernel publication bypasses the byte-channel syscall wrapper.
    publications = {(int(e['args']['queue'], 16) & ~1, e['args']['thread'])
                    for e in events if e['name'] == 'ipc_enqueue'}
    if not any(client['args']['operation'] in (5, 10)
               and (client['args']['connection'], client['args']['request']) in servers
               and (int(client['args']['connection'], 16), client['args']['thread']) in publications
               for client in clients):
        raise AssertionError('kernel-originated FAT mutation lacks matching core/server observations')
    if not any(e.get('flow_out') and e['name'] == 'RPC dispatch' for e in payload['traceEvents']):
        raise AssertionError('no remote filesystem dispatch flow')


def validate_capture(files, logfile):
    lanes = inspect_proto(files['lanes.pftrace'].read_bytes())
    if not any(one(state, 4) == 3 for _, state in lanes['states']):
        raise AssertionError('default Perfetto view lacks native scheduling states')
    if not any(thread.get(3) for thread in lanes['threads']):
        raise AssertionError('native process tree lacks thread names')
    if not any(obj['process_koid'] for obj in lanes['metadata']['thread_objects'].values()):
        raise AssertionError('scheduler ThreadIds were not joined to observed process owners')
    if any(one(e, 23) == b'sched_switch' for _, e in lanes['events']):
        raise AssertionError('default Perfetto view unexpectedly includes raw tracepoints')
    native = inspect_proto(files['native.pftrace'].read_bytes())
    outgoing = [one(e, 47) for _, e in native['events'] if 47 in e]
    incoming = [one(e, 48) for _, e in native['events'] if 48 in e]
    if not outgoing or sorted(outgoing) != sorted(incoming):
        raise AssertionError('native IPC export lacks paired causal flow IDs')
    if not any(one(e, 23) == b'RPC round trip' for _, e in native['events']):
        raise AssertionError('native IPC export lacks RPC intervals')
    scheduling = json.loads(files['ipc.json'].read_bytes())
    if not complete(scheduling):
        raise AssertionError('scheduling cause capture is incomplete')
    switches = [e for e in scheduling['traceEvents'] if e['name'] == 'sched_switch']
    annotated = [e['args'] for e in switches if e['args'].get('cause') is not None]
    if len(annotated) != len(switches) or any(
            c.get('switch_reason') in (None, 'unknown')
            or c.get('schedule_point') in (None, 'unknown')
            or c.get('preempted') is None for c in annotated):
        raise AssertionError('switch lost its packed cause or entry point')
    if not annotated or not any(c['switch_reason'] == 'block' for c in annotated):
        raise AssertionError('bounded IPC workload has no explicit blocking switch cause')
    execution_lanes = {e['pid'] for e in scheduling['traceEvents']
                       if e.get('ph') == 'M' and e.get('name') == 'process_name'
                       and e.get('args', {}).get('name') == 'HypeR thread execution'}
    if not any(e.get('ph') == 'X' and e.get('pid') in execution_lanes
               and 'cpu' in e.get('args', {})
               for e in scheduling['traceEvents']):
        raise AssertionError('missing per-thread execution lanes')
    if any(c['switch_reason'] == 'preempt' for c in annotated) and not any(
            e.get('ph') == 'X' and e.get('args', {}).get('preempted') is True
            for e in scheduling['traceEvents']):
        raise AssertionError('CPU execution slices omitted observed switch-out preemption')
    if any(e['name'] == 'sched_switch_cause' for e in scheduling['traceEvents']):
        raise AssertionError('switch cause consumed an extra ring record')
    requests = [e for e in scheduling['traceEvents'] if e['name'] == 'sched_request']
    if not any(e['args'].get('request_source') == 'wakeup' for e in requests):
        raise AssertionError('missing scheduler wakeup publication')
    if any(e['args'].get('request_source') in (None, 'unknown', 'load-balance') for e in requests):
        raise AssertionError('scheduler request source was not decoded')
    if not any(e['args'].get('irq_context') is not None for e in requests):
        raise AssertionError('no scheduling request has a confirmed enclosing IRQ')
    frozen = files['frozen.jsonl'].read_bytes()
    rows = [json.loads(line) for line in frozen.splitlines() if line.strip()]
    if not rows[1:-1] or any(event['cpu'] != 0 for event in rows[1:-1]):
        raise AssertionError('empty trace or CPU filter ignored')
    first_generation = rows[0]['generation']
    if not rows[-1]['loss']:
        raise AssertionError('tiny ring did not report overwritten events')
    if files['later.jsonl'].read_bytes() != frozen:
        raise AssertionError('stopped records changed')
    payload = json.loads(files['trace.json'].read_bytes())
    if payload['metadata']['generation'] <= first_generation:
        raise AssertionError('capture generation reused')
    events = payload['traceEvents']
    observed = {event['name'] for event in events}
    if not {'syscall_enter', 'syscall_exit'} <= observed:
        raise AssertionError(f'missing real instrumentation: {observed}')
    metadata = payload['metadata']
    loss = metadata['limited'] or any(row['overwritten'] or row['incomplete'] for row in metadata['losses'])
    if loss and metadata['cross_cpu_spans']:
        raise AssertionError('lossy capture inferred cross-CPU durations')
    # The collector must not feed itself by tracing the trace-read method.
    if any(event['name'] == 'syscall_enter' and event.get('args', {}).get('operation') in (260, 261, 262, 263)
           and event['args'].get('kind') == 19 for event in events):
        raise AssertionError('trace control recursively generated syscall events')
    logfile.with_suffix('.json').write_bytes(files['trace.json'].read_bytes())
    ipc = json.loads(files['ipc.json'].read_bytes())
    if not complete(ipc):
        raise AssertionError('IPC workload lost the complete causal stream')
    names = {event['name'] for event in ipc['traceEvents']}
    # The deliberately 128-event-limited capture can end before its first
    # context switch on one CPU. Assert scheduler coverage in the forced
    # three-thread, lossless IPC workload instead of that truncated prefix.
    required = {'sched_switch', 'ipc_enqueue', 'ipc_dequeue', 'rpc_client_begin', 'rpc_server_begin',
                'rpc_parent', 'rpc_client_end', 'rpc_server_end', 'RPC round trip', 'RPC handler'}
    if not required <= names:
        raise AssertionError(f'missing causal boundaries: {required - names}')
    parents = [event['args'] for event in ipc['traceEvents'] if event['name'] == 'rpc_parent']
    connections = {parent[key] for parent in parents for key in ('connection', 'parent_connection')}
    if len(parents) != 4 or len(connections) != 2:
        raise AssertionError('missing or ambiguous explicit two-hop workload')
    clients = {event['args']['thread'] for event in ipc['traceEvents']
               if event['name'] == 'rpc_client_begin' and event['args']['connection'] in connections}
    servers = {event['args']['thread'] for event in ipc['traceEvents']
               if event['name'] == 'rpc_server_begin' and event['args']['connection'] in connections}
    if len(clients) != 2 or len(servers) != 2 or len(clients | servers) != 3:
        raise AssertionError(f'RPC actor attribution does not match the three workload threads: {clients}, {servers}')
    flows = [event for event in ipc['traceEvents'] if event.get('flow_out')]
    for name in ('Message delivery', 'RPC dispatch', 'RPC parent', 'Wake to execution'):
        if not any(event['name'] == name for event in flows):
            raise AssertionError(f'missing observed causal flow: {name}')
    logfile.with_suffix('.ipc.json').write_bytes(files['ipc.json'].read_bytes())
    command_rows = [json.loads(line) for line in files['command.jsonl'].read_bytes().splitlines() if line.strip()]
    if not complete({'metadata': command_rows[0]}) or command_rows[-1]['loss'] or command_rows[-1]['limited']:
        raise AssertionError('command exit snapshot is incomplete')
    exits = [row['args'] for row in command_rows[1:-1]
             if row['event'] == 'syscall_exit' and row['args']['returned'] == 0]
    if not exits or any(row['status'] != 0 for row in exits):
        raise AssertionError(f'successful workload exit lost its status: {exits}')
    expiry = json.loads(files['expiry.json'].read_bytes())
    if not complete(expiry) or 'sched_switch' not in {e['name'] for e in expiry['traceEvents']}:
        raise AssertionError('deadline-only snapshot failed to drain complete frozen records')
    remote = json.loads(files['remote.json'].read_bytes())
    validate_remote(remote)
    validate_block(remote)


def validate_block(payload):
    if not complete(payload):
        raise AssertionError('block capture is incomplete')
    events = payload['traceEvents']
    names = {event['name'] for event in events}
    required = {'block_queued', 'block_origin', 'block_submit', 'block_command',
                'block_link', 'block_complete', 'block_ready', 'block_release',
                'Block batch', 'Completion retained', 'Dispatch delay'}
    if not required <= names:
        raise AssertionError(f'missing async block boundaries: {required - names}')
    for name in ('Block admission', 'Block dispatch', 'Block completion'):
        if not any(event['name'] == name and event.get('flow_out') for event in events):
            raise AssertionError(f'missing observed block flow: {name}')
    physical = [event for event in events if event['ph'] == 'X' and event.get('pid') == 4]
    if not physical or not any(event.get('args', {}).get('command', {}).get('slot') is not None
                               for event in physical):
        raise AssertionError('device command transport slot is unknown')
