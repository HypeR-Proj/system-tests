#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Kill live services and verify reports independently of the failed data path."""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

from hyper_test.command import Shell
from hyper_test.journal import Journal
from hyper_test.session import Session, native_command

CASES = ('console-raw', 'journal', 'session-manager', 'vm-manager',
         'ordinary-exit', 'ordinary-fault', 'critical-clean', 'child-failure',
         'missing-image', 'missing-terminal', 'journal-then-worker', 'terminal-disconnect',
         'terminal-shell-first')
BOARD_CASES = ('io-runtime', 'fs-backend')
ROOT = Path(__file__).resolve().parents[2]


def prepare(args, case):
    output = args.output / case
    output.mkdir(parents=True, exist_ok=False)
    system = output / 'system-root'
    shutil.copytree(args.composition / 'system-root', system, symlinks=True)
    shutil.copytree(args.composition / 'native-system', output / 'native-system')
    declarations = system / 'etc/components'
    root_path = declarations / 'root.json'
    root = json.loads(root_path.read_text())
    root['children'].append({'name': 'failure-probe', 'component': 'failure-probe'})
    program = {'image': '/bin/service-probe', 'arguments': ['fail']}
    if case in ('console-raw', 'journal', 'session-manager', 'vm-manager', *BOARD_CASES):
        program['arguments'] = ['stop-component', '/' + case]
    elif case == 'journal-then-worker':
        program['arguments'] = ['stop-component-then-report', '/journal']
    elif case == 'ordinary-fault':
        program['arguments'] = ['fault']
    elif case == 'critical-clean':
        program.update(arguments=['plain'], critical=True)
    elif case == 'child-failure':
        program['arguments'] = ['child-failure']
    elif case == 'missing-image':
        program['image'] = '/svc/does-not-exist'
    elif case == 'missing-terminal':
        # A real child exits before READY without a cooperative error report.
        (system / 'etc/hyper/session.json').write_text(json.dumps({
            'terminal': 'bin/service-probe', 'restart_limit': 0,
        }))
    (declarations / 'failure-probe.json').write_text(json.dumps({
        'name': 'failure-probe', 'program': program,
    }))
    root_path.write_text(json.dumps(root))
    if args.board:
        for name in ('service-probe', 'test-report'):
            shutil.copy2(args.apps / name, system / 'bin' / name)
        subprocess.run([
            sys.executable, '-B', str(ROOT / 'scripts/pack-board-image.py'),
            '--board', str(args.board), '--output', str(output / 'disk.img'),
            '--system-root', str(system), '--mkfs-erofs', args.mkfs_erofs,
            '--artifact', f'alpine={args.composition / "alpine.itb"}',
            '--artifact', f'alpine-rootfs={args.composition / "alpine.ext4"}',
        ], check=True)
        return args.composition / 'bootstrap.cpio'
    spec = importlib.util.spec_from_file_location(
        'fixture', Path(__file__).with_name('prepare-native-fixture.py'))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    image = output / 'failure.cpio'
    fixture.publish(output.resolve(), image.resolve(), args.packer, args.strip)
    return image


def run(args, case, initramfs):
    log = args.output / case / 'serial.log'
    killed = case in ('console-raw', 'journal', 'session-manager', 'vm-manager', *BOARD_CASES)
    critical = killed and json.loads((args.output / case / 'system-root' /
                                     'etc/components' / (case + '.json')).read_text())['program'].get('critical', False)
    command = native_command(args.qemu, args.image, initramfs)
    if args.board:
        command = [sys.executable, '-B', str(ROOT / 'scripts/run-io-vm.py'),
                   '--qemu', args.qemu, '--image', str(args.image),
                   '--initramfs', str(initramfs), '--board', str(args.board),
                   '--disk', str(args.output / case / 'disk.img')]
    with Session(command, log) as vm:
        if case == 'missing-terminal':
            vm.observe(rb'HypeR component-manager: CRITICAL: /session-manager generation=1 worker failure: .*terminal-service (?:exited|closed readiness) during startup.*ProcessExited', 120)
            print(f'service failure {case} passed: {log}', flush=True)
            return
        vm.await_text(rb'hyper-sh\$ ', timeout=180)
        shell = Shell(vm)
        journal = Journal(shell)
        journal.snapshot('/component-manager')
        boundary = vm.mark()
        if case in ('terminal-disconnect', 'terminal-shell-first'):
            vm.send(f'service-probe {case}\r'.encode())
            vm.observe(rb'HypeR component-manager: CRITICAL: /session-manager generation=1 worker failure: .*terminal-service (?:exited|closed control while releasing shell).*Requested', 30, after=boundary)
            # The old shell may have left a prompt in Session's unread buffer.
            # Wait for the new terminal epoch before addressing its shell, and
            # install the command reporter again in that fresh process.
            vm.await_text(rb'HypeR session: console ready\nhyper-sh\$ ', timeout=30)
            shell = Shell(vm)
            proof = shell.check('cat /tmp/terminal-disconnect-proof')
            if b'read=peer-closed; write=peer-closed; saved-before-retirement' not in proof:
                raise AssertionError(f'disconnected app lost its cleanup opportunity: {proof!r}')
            shell.check('echo REPLACEMENT_TERMINAL_READY')
            print(f'service failure {case} passed: {log}', flush=True)
            return
        # A dead console/session destroys the shell reply path. Acceptance must
        # use the manager's independent Console report, not that broken path.
        vm.send(b'componentctl start /failure-probe\r')
        if critical:
            vm.observe(rb'HypeR component-manager: CRITICAL: /' + case.encode()
                       + rb' generation=1 terminated: Requested; process=0x[1-9a-f][0-9a-f]*',
                       30, after=boundary)
        elif case == 'journal-then-worker':
            vm.observe(rb'HypeR component-manager: CRITICAL: /journal generation=1 terminated: Requested', 30, after=boundary)
            vm.observe(rb'HypeR component-manager: ERROR: /failure-probe generation=1 worker failure: worker failed after journal termination', 30, after=boundary)
        elif case == 'critical-clean':
            vm.observe(rb'HypeR component-manager: CRITICAL: /failure-probe generation=1 terminated: ProcessExited \{ status: 0 \}', 30, after=boundary)
        else:
            vm.await_text(rb'hyper-sh\$ ', timeout=30)
            path = '/vm-manager' if case == 'vm-manager' else '/failure-probe'
            marker = {
                'vm-manager': 'terminated: Requested',
                'ordinary-exit': 'terminated: ProcessExited { status: 1 }',
                'ordinary-fault': 'terminated: Fault {',
                'child-failure': 'worker failure: test worker terminated:',
                'missing-image': 'process start failed during prepare:',
            }[case]
            records = journal.message('/component-manager', path + ' generation=1 ' + marker)
            matching = [r for r in records if path + ' generation=1 ' + marker in r['text']]
            if len(matching) != 1 or 'ERROR:' not in matching[0]['text'] or 'process=0x' not in matching[0]['text']:
                raise AssertionError(f'missing or repeated failure identity: {matching!r}')
            status = shell.check('componentctl status ' + path)
            # Startup errors keep the launch cause rather than a synthetic exit.
            if case not in ('child-failure', 'missing-image') and marker.encode() not in status:
                raise AssertionError(f'failure reason lost from status: {status!r}')
            shell.check('echo SERVICE_FAILURE_OBSERVER_ALIVE')
    print(f'service failure {case} passed: {log}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('composition', 'output', 'image'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('packer', 'strip', 'qemu'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--case', choices=CASES + BOARD_CASES)
    parser.add_argument('--board', type=Path)
    parser.add_argument('--apps', type=Path)
    parser.add_argument('--mkfs-erofs')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.board and (args.apps is None or args.mkfs_erofs is None):
        parser.error('--board requires --apps and --mkfs-erofs')
    if args.case in BOARD_CASES and not args.board:
        parser.error('physical provider cases require --board')
    for case in (args.case,) if args.case else (BOARD_CASES if args.board else CASES):
        run(args, case, prepare(args, case))


if __name__ == '__main__':
    main()
