#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Import an exported timeline with an upstream Perfetto trace_processor binary.

No downloads, UI automation or synthetic Linux ftrace records. Exported flow
counts must agree with Perfetto's relational flow table. Native scheduling must
populate sched and thread_state, with no importer error/data-loss diagnostics.
Source capture loss remains separate from import errors.
"""

import argparse
import csv
import io
import json
from pathlib import Path
import subprocess
from perfetto_proto import inspect as inspect_proto, one


def query_rows(processor, trace, statement):
    result = subprocess.run([str(processor), str(trace), '-Q', statement],
                            capture_output=True, text=True, check=True, timeout=120)
    rows = list(csv.DictReader(io.StringIO(result.stdout.strip())))
    return rows, result.stderr


def query(processor, trace, statement):
    rows, diagnostics = query_rows(processor, trace, statement)
    if len(rows) != 1:
        raise ValueError(f'unexpected trace_processor result: {rows}')
    return rows[0], diagnostics


def verify(processor, trace, *, scheduling=False):
    if not trace.read_bytes().startswith(b'{'):
        return verify_native(processor, trace, scheduling=scheduling)
    payload = json.loads(trace.read_text())
    events = payload['traceEvents']
    if scheduling:
        expected = [e for e in events if e.get('ph') == 'X' and e.get('pid') == 5]
        if not expected:
            raise ValueError('expected nonempty thread execution windows')
        statement = """
        SELECT
          (SELECT count(*) FROM slice s JOIN thread_track tt ON tt.id = s.track_id
           JOIN thread t ON t.utid = tt.utid JOIN process p ON p.upid = t.upid
           WHERE p.pid = 5) AS runs,
          (SELECT coalesce(sum(value), 0) FROM stats
           WHERE severity IN ('error', 'data_loss')) AS errors;
        """
        row, diagnostics = query(processor, trace, statement)
        if int(row['runs']) != len(expected) or int(row['errors']):
            raise ValueError(f'Perfetto scheduling import mismatch: {row}; expected {len(expected)}\n{diagnostics}')
        print(f'Perfetto import passed: {len(expected)} thread execution windows, no importer errors')
        return
    starts = [event for event in events if event.get('flow_out')]
    ends = [event for event in events if event.get('flow_in')]
    if not starts or sorted(e['bind_id'] for e in starts) != sorted(e['bind_id'] for e in ends):
        raise ValueError('expected nonempty, paired exported flows')
    statement = """
    SELECT
      (SELECT count(*) FROM flow) AS flows,
      (SELECT count(*) FROM slice WHERE name = 'RPC round trip') AS rpc,
      (SELECT coalesce(sum(value), 0) FROM stats
       WHERE severity IN ('error', 'data_loss')) AS errors;
    """
    row, diagnostics = query(processor, trace, statement)
    expected_rpc = sum(e.get('ph') == 'X' and e['name'] == 'RPC round trip' for e in events)
    if int(row['flows']) != len(starts) or int(row['rpc']) != expected_rpc or int(row['errors']):
        raise ValueError(f'Perfetto import mismatch: {row}; expected flows={len(starts)}, RPC={expected_rpc}\n{diagnostics}')
    print(f'Perfetto import passed: {row["flows"]} causal flows, {row["rpc"]} RPC intervals, no importer errors')


def verify_native(processor, trace, *, scheduling=False):
    source = inspect_proto(trace.read_bytes())
    starts = [one(e, 47) for _, e in source['events'] if 47 in e]
    ends = [one(e, 48) for _, e in source['events'] if 48 in e]
    if sorted(starts) != sorted(ends):
        raise ValueError('unpaired native causal flow IDs')
    expected_runs = sum(one(s, 4) == 3 for _, s in source['states'])
    expected_rpc = sum(one(e, 23) == b'RPC round trip' for _, e in source['events'])
    statement = """
    SELECT
      (SELECT count(*) FROM sched) AS runs,
      (SELECT count(*) FROM sched WHERE dur < 0) AS open_runs,
      (SELECT count(*) FROM thread_state WHERE state = 'Running') AS running,
      (SELECT count(*) FROM flow) AS flows,
      (SELECT count(*) FROM slice WHERE name = 'RPC round trip') AS rpc,
      (SELECT coalesce(sum(value),0) FROM stats
       WHERE severity IN ('error','data_loss')) AS errors;
    """
    row, diagnostics = query(processor, trace, statement)
    if (int(row['runs']) != expected_runs or int(row['open_runs'])
            or int(row['running']) != expected_runs or int(row['errors'])
            or int(row['flows']) != len(starts) or int(row['rpc']) != expected_rpc):
        raise ValueError(f'Native Perfetto import mismatch: {row}; expected runs={expected_runs}, flows={len(starts)}, RPC={expected_rpc}\n{diagnostics}')
    rows, _ = query_rows(processor, trace, """
        SELECT t.tid,t.name,p.pid,p.name AS process_name,t.is_idle,t.end_ts
        FROM thread t LEFT JOIN process p USING(upid);
    """)
    actual = {}
    for thread in rows:
        # KOIDs preserve all 64 bits; protobuf/SQL int64 renders bit 63 as a sign.
        actual.setdefault(int(thread['tid']) & ((1 << 64) - 1), []).append(thread)
    destroyed = {one(s, 3): ts for ts, s in source['states'] if one(s, 4) == 8}
    for tid, end in destroyed.items():
        matches = actual.get(tid, [])
        if len(matches) != 1 or matches[0]['end_ts'] != str(end):
            raise ValueError(f'Perfetto lost thread retirement: tid={tid}, end={end}, imported={matches}')
    zombies, _ = query_rows(processor, trace, """
        SELECT t.tid,s.ts,s.dur FROM thread_state s JOIN thread t USING(utid)
        WHERE s.state = 'Z';
    """)
    for zombie in zombies:
        end = destroyed.get(int(zombie['tid']) & ((1 << 64) - 1))
        if end is not None and int(zombie['dur']) != end - int(zombie['ts']):
            raise ValueError(f'Perfetto did not close Zombie at retirement: {zombie}, end={end}')
    from perfetto_proto import fields
    processes = {one(p, 1): one(p, 3).decode()
                 for _, tree in source['trees'] for p in map(fields, tree.get(1, []))}
    for thread in source['threads']:
        tid, pid, name = one(thread, 1), one(thread, 2), one(thread, 3).decode()
        matches = actual.get(tid, [])
        if len(matches) != 1 or (int(matches[0]['pid']), matches[0]['name'], matches[0]['process_name'],
                                 int(matches[0]['is_idle'])) != (pid, name, processes[pid], one(thread, 5, 0)):
            raise ValueError(f'Perfetto changed thread ownership: tid={tid}, pid={pid}, name={name}, imported={matches}')
    if scheduling and not expected_runs:
        raise ValueError('native trace has no scheduling observations')
    print(f'Native Perfetto import passed: {expected_runs} sched/thread-state intervals, {len(starts)} flows, {expected_rpc} RPC intervals, no importer errors')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    parser.add_argument('--processor', type=Path, required=True)
    parser.add_argument('--scheduling', action='store_true',
                        help='verify thread execution lanes instead of requiring causal flows')
    args = parser.parse_args()
    verify(args.processor.resolve(), args.trace.resolve(), scheduling=args.scheduling)


if __name__ == '__main__':
    main()
