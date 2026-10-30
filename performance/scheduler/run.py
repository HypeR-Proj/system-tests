#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Run the scheduling suite on an owned QEMU; retrieve JSON through a FAT volume."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tests/qemu'))
from hyper_test.command import Shell
from hyper_test.guest_console import append_console_output
from hyper_test.session import Session
from trace_artifacts import TraceArtifacts, boot_command
from report import validate, write_csv
from capacity import check_export_capacity, export_budget


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def execute(args):
    if not args.functional and (os.environ.get('CI') == 'true' or os.environ.get('GITHUB_ACTIONS') == 'true'):
        raise ValueError('CI may run --functional only; latency thresholds are not CI gates')
    if not 1 <= args.cpus <= 64:
        raise ValueError('select 1..64 QEMU CPUs')
    if not 1 <= args.duration_ms <= 3_600_000 or not 1 <= args.repeat <= 100:
        raise ValueError('duration must be 1..3600000 ms and repeat 1..100')
    args.output.mkdir(parents=True, exist_ok=False)
    log = args.log or args.output / 'serial.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    os.environ['QEMU_CPUS'] = str(args.cpus)
    command = boot_command(args.qemu, args.image, args.initramfs, args.disk, args.board)
    manifest = dict(mode='functional only' if args.functional else 'manual TCG measurement',
                    source_revision=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                    source_dirty=bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
                    host=platform.platform(), qemu_version=subprocess.check_output([str(args.qemu), '--version'], text=True),
                    command=command, environment={k: v for k, v in os.environ.items() if k.startswith('QEMU_')},
                    artifacts={str(p): digest(p) for p in (args.image, args.initramfs, args.board)}, commands=[])
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    # Never extract a disk while the emulator can write to it. Logs/results are
    # copied after every timed phase, then fenced by unmount and emulator exit.
    artifacts = TraceArtifacts(args.disk, args.board, args.output)
    duration = 300 if args.functional else args.duration_ms
    iterations = ' --iterations 8' if args.functional else ''
    common = (f'--cpus 0-{args.cpus - 1} --duration-ms {duration} '
              f'--warmup {2 if args.functional else 20} --histogram-bins {128 if args.functional else 32768} '
              f'--raw --max-raw {2 if args.functional else 10000}')
    cases = [('baseline.json', 'suite', iterations),
             ('cpu-load.json', 'cyclic', '--load-threads 2' + iterations),
             ('memory-load.json', 'cyclic', '--load-threads 1 --load memory --load-mib 2' + iterations),
             ('catch-up.json', 'cyclic', '--overrun catch-up' + iterations),
             ('lower-weight.json', 'clock', '--policy fair --weight 1' + iterations)]
    names = [name for name, _, _ in cases]
    repeats = [(2 if name == 'catch-up.json' else 1) if args.functional else args.repeat
               for name in names]
    # Suite has 11 series/CPU, plus 2 for the optional cross-CPU pair.
    # Four additional single-series cases share the same storage geometry.
    per_cpu = [13 if args.cpus > 1 else 11, 1, 1, 1, 1]
    series = args.cpus * sum(n * repeat for n, repeat in zip(per_cpu, repeats))
    required = export_budget(series, 128 if args.functional else 32768,
                             2 if args.functional else 10000)
    manifest['export_capacity'] = check_export_capacity(artifacts, required)
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    with Session(command, log, output_filter=append_console_output) as session:
        session.await_text(rb'HypeR: startup complete\n', timeout=240)
        session.await_text(rb'hyper-sh\$ ')
        shell = Shell(session)
        for (name, case, extra), repeat in zip(cases, repeats):
            source = f'/bin/sched-bench {case} {common} {extra} --repeat {repeat} --output /tmp/{name}'
            manifest['commands'].append(source)
            (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            shell.check(source, timeout=180 if args.functional else 3600)
        if args.functional:
            for extra in ('--interval-us 0', '--cpus 0,255', '--policy fifo', '--lock-memory',
                          '--worker-image /missing-scheduler-worker', '--output /tmp/baseline.json'):
                shell.check('/bin/sched-bench clock --duration-ms 10 ' + extra, failed=True)
            processes = shell.check('ps')
            if b'sched-Clock-' in processes or b'sched-Channel-' in processes:
                raise AssertionError('failed benchmark left a running worker')
            shell.check('echo SCHEDULER_SUITE_RESPONSIVE')
        TraceArtifacts.save(shell, names)
    files = artifacts.extract(session, names)
    for path in files.values():
        encoded = path.read_bytes()
        if b'\n' in encoded.removesuffix(b'\n'):
            raise AssertionError('machine report is not compact JSON; export bound must be updated')
        validate(json.loads(encoded))
    actual_bytes = sum(path.stat().st_size for path in files.values())
    if actual_bytes > required - 8 * 1024 * 1024:
        raise AssertionError('machine reports exceeded the conservative export bound')
    manifest['export_capacity']['actual_report_bytes'] = actual_bytes
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if args.functional:
        catch_up = json.loads(files['catch-up.json'].read_bytes())
        if {r['job']['repetition'] for r in catch_up['results']} != {0, 1}:
            raise AssertionError('repeated measurement did not preserve both runs')
        lower = json.loads(files['lower-weight.json'].read_bytes())
        if any('weight: 1,' not in s['scheduling'] for r in lower['results'] for s in r['series']):
            raise AssertionError('self-lowering policy was not applied to measured threads')
    baseline = json.loads(files['baseline.json'].read_bytes())
    expected = {'clock', 'cyclic', 'yield', 'atomic', 'channel', 'fairness', 'spawn'}
    if {r['job']['case'] for r in baseline['results']} != expected:
        raise AssertionError('suite omitted a required case')
    for result in baseline['results']:
        if args.functional and any(s['work_units'] == 0 for s in result['series']):
            raise AssertionError('functional case made no progress')
        if result['job']['case'] == 'channel' and result['job']['options']['cross_cpu']:
            if result['job']['cpu'] == result['job']['peer_cpu']:
                raise AssertionError('cross-CPU label has same-CPU placement')
    if args.cpus > 1 and not any(r['job']['options']['cross_cpu'] for r in baseline['results']):
        raise AssertionError('multi-CPU suite omitted cross-CPU IPC')
    write_csv(list(files.values()), args.output / 'summary.csv')
    print(f'Scheduler suite passed; raw evidence: {args.output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    for name in ('image', 'initramfs', 'disk', 'board', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--log', type=Path)
    parser.add_argument('--cpus', type=int, default=4)
    parser.add_argument('--duration-ms', type=int, default=1000)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--functional', action='store_true')
    args = parser.parse_args()
    # A bare executable name must retain PATH resolution rather than becoming
    # relative to the source directory through pathlib's string conversion.
    execute(args)


if __name__ == '__main__':
    main()
