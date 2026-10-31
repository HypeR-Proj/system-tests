#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Nested capability routes, concurrent lazy activation, stdio and bounded lifecycle."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import time

from hyper_test.session import Session, native_command
from hyper_test.command import Shell
from hyper_test.journal import Journal


def prepare(args):
    args.output.mkdir(parents=True, exist_ok=False)
    system = args.output / 'system-root'
    shutil.copytree(args.composition / 'system-root', system, symlinks=True)
    shutil.copytree(args.composition / 'native-system', args.output / 'native-system')
    # Configuration is policy, not a fixed directory inside the manager.
    declarations = system / 'etc/test-components'
    (system / 'etc/components').rename(declarations)
    manager = system / 'etc/hyper/components.json'
    config = json.loads(manager.read_text())
    config['configuration_directory'] = '/etc/test-components'
    manager.write_text(json.dumps(config))
    root_path = declarations / 'root.json'
    root = json.loads(root_path.read_text())
    policy_path = system / 'etc/hyper/startup.json'
    policy = json.loads(policy_path.read_text())
    # Omitting a domain service closes its old rendezvous instead of hanging vmm.
    root['children'] = [c for c in root['children'] if c['name'] != 'vm-manager']
    policy['applications'] = [a for a in policy['applications'] if a['component'] != '/vm-manager']
    (declarations / 'vm-manager.json').unlink()
    root['children'].append({'name': 'tests', 'component': 'tests'})
    # Native ABI CapabilityChannel with DUPLICATE|TRANSFER|WAIT|WRITE.
    observe = {'type': 'object', 'object_type': 14, 'rights': 39}
    root.setdefault('capabilities', []).append({'name': 'observe', 'kind': observe})
    root.setdefault('offer', []).append({'name': 'observe', 'to': 'tests', 'from': 'self', 'source': 'observe', 'kind': observe})
    policy['exports'] = [{'realm': '/', 'name': 'observe', 'source': 'component.observe'}]
    realm = {'name': 'tests', 'children': [], 'offer': []}
    protocol = {'type': 'protocol', 'protocol': 'test.Echo'}
    def component(name, mode, **options):
        realm['children'].append({'name': name, 'component': name})
        value = {'name': name, 'program': {'image': '/bin/service-probe', 'arguments': [mode], **options}}
        return value
    def write(value):
        (declarations / (value['name'] + '.json')).write_text(json.dumps(value))
    for name, mode in [('plain', 'plain'), ('ipc', 'ipc'), ('observer', 'observe')]:
        value = component(name, mode, startup='eager')
        if name == 'observer':
            value['use'] = [{'name': 'observe', 'kind': observe, 'purpose': 0x800c0003}]
            realm['offer'].append({'name': 'observe', 'to': name, 'from': 'parent', 'source': 'observe', 'kind': observe})
        write(value)
    for name, mode in [('server', 'echo'), ('silent', 'idle')]:
        value = component(name, mode)
        value['capabilities'] = [{'name': 'echo', 'kind': protocol}]
        value['expose'] = [{'name': 'echo', 'from': 'self', 'source': 'echo', 'kind': protocol}]
        write(value)
    for name in ('burst-a', 'burst-b'):
        value = component(name, 'journal-burst')
        value['program']['arguments'].append(name)
        write(value)
    for name, mode, provider in [('client', 'connect', 'server'), ('timeout', 'timeout', 'silent')]:
        value = component(name, mode)
        value['use'] = [{'name': 'echo', 'kind': protocol}]
        realm['offer'].append({'name': 'echo', 'to': name, 'from': '#' + provider, 'source': 'echo', 'kind': protocol})
        write(value)
    write(component('retry', 'fail', restart='on-failure', restart_delay_ms=100, restart_limit=2))
    for prepared in (False, True):
        for resident in (False, True):
            result = 'allowed' if resident else 'denied'
            name = ('prepared-' if prepared else '') + 'resident-' + result
            value = component(name, 'memory-lock', startup='eager', prepared=prepared, resident=resident)
            value['program']['arguments'].append(result)
            write(value)
            if prepared:
                policy['applications'].append({'component': '/tests/' + name, 'critical': True})
    write(realm)
    if args.mode == 'prepare-rejected-console':
        provider = next(p['component'] for p in policy['providers'] if p['role'] == 'console')
        declaration = next(c['component'] for c in root['children'] if '/' + c['name'] == provider)
        path = declarations / (declaration + '.json')
        console = json.loads(path.read_text())
        console['program']['image'] = '/svc/missing-console-raw'
        path.write_text(json.dumps(console))
    root_path.write_text(json.dumps(root))
    policy_path.write_text(json.dumps(policy))
    spec = importlib.util.spec_from_file_location('fixture', Path(__file__).with_name('prepare-native-fixture.py'))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    fixture.publish(args.output.resolve(), (args.output / 'services.cpio').resolve(), args.packer, args.strip)


def run_rejected_console(args):
    with Session(native_command(args.qemu, args.image, args.initramfs), args.log) as vm:
        boot = vm.await_text(rb'hyper-sh\$ ', timeout=90)
        if (b'HypeR: startup incomplete' not in boot
                or b'provider adoption failed: bootstrap retirement disabled' not in boot
                or b'bootstrap retired; boot archive detached' in boot):
            raise AssertionError('console adoption failure lost diagnostics or retired live owners')
        vm.send(b'echo CONSOLE_REPLACEMENT_RECOVERY_OK\n')
        vm.await_text(rb'(?m)^CONSOLE_REPLACEMENT_RECOVERY_OK\n', timeout=15)
        vm.await_text(rb'hyper-sh\$ ', timeout=15)
    print(f'console replacement recovery passed: {args.log}')


def run(args):
    with Session(native_command(args.qemu, args.image, args.initramfs), args.log) as vm:
        boot = vm.await_text(rb'hyper-sh\$ ', timeout=180)
        start = boot.index(b'HypeR component manager /')
        release = boot.index(b'HypeR: startup complete')
        prompt = boot.index(b'hyper-sh$ ')
        if not start < release < prompt:
            raise AssertionError('startup display/console/shell handoff is out of order')
        # Eager processes may finish after startup. Their log delivery is
        # verified through the journal below, independently of splash timing.
        for marker in (b'/console', b'/journal', b'/session-manager'):
            if marker not in boot[start:release]:
                raise AssertionError(f'early display missed component startup: {marker!r}')
        if b'HypeR session-manager: session ended:' in boot:
            raise AssertionError(f'initial session needed recovery: {boot!r}')
        shell = Shell(vm)
        journal = Journal(shell)
        command = shell.check
        def state(name, expected, instance=None):
            until = time.monotonic() + 30
            while time.monotonic() < until:
                output = command(f'componentctl status {name}')
                if re.search(re.escape(name.encode()) + rb'\s+' + expected.encode() + rb'\s', output):
                    if instance is None or re.search(re.escape(name.encode()) + rb'\s+' + expected.encode() + rb'\s+' + str(instance).encode() + rb'\s', output):
                        return output
                vm.pump(0.1)
            raise AssertionError(f'{name} did not reach {expected}: {output!r}')
        plain = state('/tests/plain', 'completed')
        if b'terminated: ProcessExited { status: 0 }' not in plain:
            raise AssertionError(f'normal exit lost its kernel termination reason: {plain!r}')
        completed = journal.message('/component-manager', '/tests/plain generation=1 exited successfully')
        reports = [r['text'] for r in completed if '/tests/plain generation=1 ' in r['text']]
        if len(reports) != 1 or not reports[0].startswith('INFO: ') or 'status 0' not in reports[0]:
            raise AssertionError(f'normal completion has no informational report: {completed!r}')
        state('/tests/observer', 'completed')
        state('/tests/ipc', 'completed')
        if b'interactive-ready' not in command('componentctl status /session-manager'):
            raise AssertionError('interactive program did not report readiness')
        for prefix in ('', 'prepared-'):
            for result in ('allowed', 'denied'):
                path = '/tests/' + prefix + 'resident-' + result
                state(path, 'completed')
                journal.message(path, 'COMPONENT_MEMORY_LOCK_' + result.upper() + '_OK')
        state('/tests/server', 'stopped', 0)
        unavailable = shell.check('vmm list', failed=True, timeout=10)
        if b'Status(Status(-15))' not in unavailable:
            raise AssertionError(f'absent manager did not close its rendezvous: {unavailable!r}')
        journal.message('/tests/plain', 'SERVICE_STDOUT_OK', stream='stdout')
        journal.message('/tests/plain', 'SERVICE_STDERR_OK', stream='stderr')
        journal.message('/tests/observer', 'SERVICE_OBSERVER_DENIED_OK')
        journal.message('/tests/ipc', 'SERVICE_LARGE_REPLY_OK')
        command('componentctl start /tests/burst-a; componentctl start /tests/burst-b')
        for name in ('burst-a', 'burst-b'):
            state('/tests/' + name, 'completed')
            records = journal.wait('/tests/' + name, lambda items: len(items) >= 194)
            if len(records) != 194:
                raise AssertionError(f'unexpected journal records for {name}: {records!r}')
            for stream, tag in (('stdout', 'out'), ('stderr', 'err')):
                actual = [(r['text'], r['continued'], r['source']['generation'])
                          for r in records if r['source']['stream'] == stream]
                expected = [(f'JOURNAL_BURST {name} {tag} {i:03}', False, 1) for i in range(96)]
                expected.append((f'JOURNAL_BURST {name} {tag} EOF', True, 1))
                if actual != expected:
                    raise AssertionError(f'batch order, framing or attribution changed: {actual!r}')
        command('componentctl routes /tests/client echo')
        for generation in (1, 2):
            command('componentctl start /tests/client')
            state('/tests/client', 'completed', generation)
            state('/tests/server', 'running', 1)
        for generation in (1, 2):
            journal.message('/tests/client', 'COMPONENT_CONCURRENT_CONNECT_OK', generation=generation)
            records = journal.message('/tests/client', 'COMPONENT_UNDECLARED_DENIED_OK', generation=generation)
        for marker in ('COMPONENT_CONCURRENT_CONNECT_OK', 'COMPONENT_UNDECLARED_DENIED_OK'):
            if sum(marker in record['text'] for record in records) != 2:
                raise AssertionError(f'client log duplicated an outcome: {records!r}')
        command('componentctl restart /tests/server')
        state('/tests/server', 'running', 2)
        command('componentctl start /tests/client')
        state('/tests/client', 'completed', 3)
        command('componentctl stop /tests/server')
        state('/tests/server', 'stopped', 2)
        command('componentctl start /tests/timeout')
        state('/tests/timeout', 'completed', 1)
        journal.message('/tests/timeout', 'COMPONENT_DEADLINE_OK')
        command('componentctl stop /tests/silent')
        state('/tests/silent', 'stopped', 1)
        command('componentctl start /tests/retry')
        state('/tests/retry', 'failed', 3)
        for generation in (1, 2, 3):
            records = journal.message('/tests/retry', 'SERVICE_FAILURE_ATTEMPT', generation=generation, stream='stderr')
        if sum(record['text'] == 'SERVICE_FAILURE_ATTEMPT' for record in records) != 3:
            raise AssertionError(f'retry budget or attribution failed: {records!r}')
        denied = shell.check('componentctl stop /session-manager', failed=True, timeout=10)
        if b'ownership retirement protocol' not in denied:
            raise AssertionError(f'stateful stop was not rejected: {denied!r}')
        command('echo COMPONENT_MANAGEMENT_OK')
    print(f'component management acceptance passed: {args.log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('prepare', aliases=['prepare-rejected-console'])
    for name in ('composition', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--packer', required=True)
    p.add_argument('--strip', required=True)
    p = sub.add_parser('run', aliases=['run-rejected-console'])
    p.add_argument('--qemu', required=True)
    for name in ('image', 'initramfs', 'log'):
        p.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    {'prepare': prepare, 'run': run,
     'prepare-rejected-console': prepare, 'run-rejected-console': run_rejected_console}[args.mode](args)


if __name__ == '__main__':
    main()
