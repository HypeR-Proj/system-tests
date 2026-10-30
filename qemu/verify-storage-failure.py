#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Keep Native recovery tools usable when optional bootstrap storage fails."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from hyper_test.session import Session, native_command
from hyper_test.command import Shell
from hyper_test.journal import Journal
from cpio_fixture import replace_file

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from board_bootstrap import services
from board_config import Board
from native_deployment import prepare as prepare_layout

CASES = ('missing-controller', 'provider-exit', 'closed-ready', 'invalid-ready',
         'timeout', 'missing-config', 'oversized-ready', 'launch-failure', 'critical-exit',
         'root-change', 'root-switch-failure', 'critical-exit-after-ready', 'missing-system-shell',
         'handoff-failure')
DEGRADED = b'HypeR system-startup: VM fleet configuration unavailable; Native services remain available'
ADOPTED = b'HypeR system-startup: core supervision adopted'
# The journal carries the component-manager source separately from the message.
CRITICAL_TERMINATED = rb'/vm-manager generation=1 terminated: ProcessExited \{ status: 1 \}'
LIVE_PROVIDERS = frozenset(('closed-ready', 'invalid-ready', 'oversized-ready',
                            'timeout', 'missing-config', 'root-change'))
BOOT_FAILED = b'[HypeR bootstrap stage 1][init] bootstrap failed'
INIT_FAILURES = (BOOT_FAILED, b'HypeR system-startup: bootstrap failed',
                 b'[HypeR bootstrap stage 1][init] critical service ',
                 b'HypeR system-startup: critical service ')
KERNEL_FAILURES = (b'HypeR: fatal', b'kernel panic', b'HypeR crash monitor')
# Successful readiness fixtures keep the private migration endpoint open but
# do not implement adoption. The coordinator exhausts the provider's configured
# deadline, then starts the independently backed system services.
FORMAL_STARTUP_TIMEOUT = 120


def prepare(args):
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    board = Board.parse(prepare_layout(
        json.loads((ROOT / 'boards/qemu.json').read_text())))
    board_path = args.output / 'board.json'
    board_path.write_text(json.dumps(board.source, indent=2) + '\n')
    clients = args.output / 'io-clients.conf'
    clients.write_text(board.bootstrap_clients())
    storage = args.output / 'storage.json'
    storage.write_text(json.dumps(board.storage_policy(), indent=2) + '\n')
    for mode in range(6):
        subprocess.run([str(args.sdk / 'bin/hyper-clang'), '-std=c17', '-Wall',
                        '-Wextra', '-Werror', '-static', f'-DREADINESS_TEST={mode}',
                        str(ROOT / 'tests/native/storage-readiness.c'),
                        '-o', str(args.output / f'provider-{mode}')], check=True)
    static_echo = args.output / 'recovery-echo'
    subprocess.run([str(args.sdk / 'bin/hyper-clang'), '-std=c17', '-Wall',
                    '-Wextra', '-Werror', '-static',
                    str(ROOT / 'tests/native/static-echo.c'),
                    '-o', str(static_echo)], check=True)
    late_manager = args.output / 'late-critical-manager'
    subprocess.run([str(args.sdk / 'bin/hyper-clang'), '-std=c17', '-Wall',
                    '-Wextra', '-Werror', '-static', '-DVM_PROVISION_TEST=4',
                    str(ROOT / 'tests/native/vm-provisioning.c'),
                    '-o', str(late_manager)], check=True)
    empty_fleet = args.output / 'empty-fleet.json'
    empty_fleet.write_text(json.dumps({'format': 'hyper.vm-config', 'virtual-machines': []}) + '\n')
    subprocess.run([str(args.sdk / 'bin/hyper-clang'), '-std=c17', '-Wall',
                    '-Wextra', '-Werror', '-static', str(ROOT / 'tests/native/root-switch.c'),
                    '-o', str(args.output / 'root-observer')], check=True)
    for case in (args.case,) if args.case else CASES:
        manifest = args.output / f'{case}.json'
        graph = services(board, ROOT)
        source = next(item for item in graph['services'] if item['name'] == 'io-runtime')
        synthetic = case != 'missing-controller'
        if synthetic:
            source['image'] = '/svc/storage-probe'
            source['capabilities'] = [grant for grant in source['capabilities']
                                      if grant['purpose'] != 'vm.creation-authority']
        product = args.output / f'{case}-product.config'
        policy = args.output / f'{case}-boot-policy.json'
        policy.write_text(json.dumps({
            'storage-providers': [{'component': '/io-runtime', 'image': '/svc/io-runtime'}],
            'boot-profiles': {'default': {
                'version': 1, 'services': '/etc/hyper/services.json',
                'io': {'kind': 'service' if synthetic else 'io-vm', 'service': 'io-runtime',
                       'successor': '/io-runtime'},
                'handoff': {'system-root': '/system', 'put-old': '/boot',
                            'system-init': '/svc/component-manager'},
            }},
        }, indent=2) + '\n')
        product.write_text('CONFIG_BOOT_POLICY=' + json.dumps(str(policy.resolve())) + '\n')
        if case in ('critical-exit', 'critical-exit-after-ready'):
            next(item for item in graph['services']
                 if item['name'] == 'vm-manager')['critical'] = True
        if case in ('critical-exit-after-ready', 'missing-system-shell'):
            graph['virtual-machines']['config'] = '/etc/hyper/vms.json'
        manifest.write_text(json.dumps(graph, indent=2) + '\n')
        provider = (args.boot_apps or args.apps) / 'io-runtime'
        replacements = []
        providers = ('provider-exit', 'closed-ready', 'invalid-ready', 'timeout',
                     'missing-config', 'oversized-ready')
        if case in providers:
            provider = args.output / f'provider-{providers.index(case)}'
        elif case == 'root-change':
            provider = args.output / 'root-observer'
        elif case in ('root-switch-failure', 'handoff-failure'):
            provider = args.output / 'provider-4'
        elif case == 'launch-failure':
            # Admit a valid static provider first; damage its packed bytes below.
            provider = args.output / 'provider-0'
        elif case == 'critical-exit':
            # Reach the committed system-init handoff without waiting for the
            # storage deadline; the critical system service then exits itself.
            provider = args.output / 'provider-4'
            replacements = ['--replace', f'svc/vm-manager={args.output / "provider-0"}']
        elif case in ('critical-exit-after-ready', 'missing-system-shell'):
            provider = args.output / 'provider-4'
            replacements = ['--replace', f'svc/vm-manager={late_manager}']
        composition = args.output / case
        subprocess.run([
            sys.executable, '-B', str(ROOT / 'scripts/pack-native-system.py'),
            '--packer', args.packer, '--strip', args.strip,
            '--output', str(composition),
            '--initramfs', str(args.output / f'{case}.cpio'),
            '--services', str(manifest),
            '--system-config', str(product),
            '--deployment', str(ROOT / 'mk/components.mk'), '--profile', 'system',
            '--apps', str(args.apps), '--boot-apps', str(args.boot_apps or args.apps),
            '--sdk', str(args.sdk), '--std', str(args.output),
            '--arch', 'aarch64', '--replace', f'init={args.fixture_init}', *replacements,
            '--boot-components', *args.boot_components, '--',
            '0755', 'boot:' + source['image'].lstrip('/'), str(provider.resolve()),
            '0755', 'boot:bin/recovery-echo', str(static_echo),
            '0755', 'system:bin/test-report', str(args.apps / 'test-report'),
            '0644', 'etc/hyper/board.json', str(board_path),
            '0644', 'etc/hyper/io-clients.conf', str(clients),
            '0644', 'etc/hyper/storage.json', str(storage),
            *(['0644', 'vm/io.itb', str(args.io_image.resolve())] if not synthetic else []),
            *(['0644', 'system:etc/hyper/vms.json', str(empty_fleet)]
              if case in ('critical-exit-after-ready', 'missing-system-shell') else []),
        ], check=True)
        # Product composition keeps the trees separate. Only this test-only
        # init copies the formal payload to an independent RAMFS before stage1.
        # No fake storage provider is asked to supply an actual root filesystem.
        (composition / 'system-root/etc/fstab').write_text('')
        if case == 'critical-exit':
            # The provider never sends its adoption description. A short program
            # timeout must override the old hardcoded 60-second adapter wait.
            path = composition / 'system-root/etc/components/io-runtime.json'
            component = json.loads(path.read_text())
            component['program']['timeout_ms'] = 1000
            path.write_text(json.dumps(component, indent=2) + '\n')
        if case in ('handoff-failure', 'root-switch-failure'):
            boot_entries = composition / 'native-system/boot.entries.json'
            entries = json.loads(boot_entries.read_text())
            record, = (entry for entry in entries if entry[1] == 'etc/hyper/boot-profiles/default.json')
            policy = json.loads(Path(record[2]).read_text())
            if case == 'handoff-failure':
                policy['handoff']['system-init'] = '/bin/echo'
            else:
                policy['handoff']['put-old'] = '/missing-root-mountpoint'
            changed = composition / 'failure-profile.json'
            changed.write_text(json.dumps(policy, indent=2) + '\n')
            record[2] = str(changed.resolve())
            boot_entries.write_text(json.dumps(entries, indent=2) + '\n')
        elif case == 'missing-system-shell':
            (composition / 'system-root/bin/sh').unlink()
        subprocess.run([
            sys.executable, '-B', str(ROOT / 'tests/qemu/prepare-native-fixture.py'),
            '--composition', str(composition), '--output', str(args.output / f'{case}.cpio'),
            '--packer', args.packer, '--strip', args.strip,
        ], check=True)
        if case == 'launch-failure':
            replace_file(args.output / f'{case}.cpio', 'svc/storage-probe',
                         b'Invalid optional provider image\n')


def shell(session, command, *, recovery=False):
    if recovery:
        session.send(command + b"\n/bin/recovery-echo STORAGE-''COMMAND-DONE\n")
        output = session.await_text(rb'STORAGE-COMMAND-DONE\n')
        session.await_text(rb'fallback-sh\$ ')
        failed = b'sh: ' in output
    else:
        result = session.shell.run(command)
        output = b'\n' + result.output
        failed = result.status != 0
    if failed:
        raise AssertionError(f'Native command failed: {output!r}')
    return output


def run_case(args, case):
    logfile = args.output / f'{case}.log'
    # Deliberately no physical storage or network controller. The first case
    # uses the real io-runtime; the rest isolate readiness and launch failures.
    command = native_command(args.qemu, args.image, args.output / f'{case}.cpio')
    failures = KERNEL_FAILURES + (b'ROOT-SWITCH: FAIL',)
    if case not in ('critical-exit', 'critical-exit-after-ready', 'missing-system-shell'):
        failures += INIT_FAILURES
    if case in ('handoff-failure', 'root-switch-failure'):
        failures += (b'core supervision transferred', b'core supervision adopted')
    with Session(command, logfile, failures=failures) as session:
        session.shell = Shell(session)
        def formal_prompt(timeout=60):
            session.await_text(rb'hyper-sh\$ ', timeout)

        if case == 'handoff-failure':
            session.await_text(rb'\[HypeR bootstrap stage 1\]\[fallback-sh\] recovery console ready', 180)
            session.await_text(rb'fallback-sh\$ ')
            # A non-reading child must not steal subsequent pasted commands.
            session.send(b'/bin/recovery-echo RECOVERY_ONE\r/bin/recovery-echo RECOVERY_TWO\r')
            session.await_text(rb'(?m)^RECOVERY_ONE\n', 30)
            session.await_text(rb'(?m)^RECOVERY_TWO\n', 30)
            session.await_text(rb'fallback-sh\$ ')
            session.send(b'exit\r')
            session.await_text(rb'\[HypeR bootstrap stage 1\]\[fallback-sh\] recovery console ready', 30)
            session.await_text(rb'fallback-sh\$ ')
            shell(session, b'/bin/recovery-echo RAW_RECOVERY_RESTARTED', recovery=True)
            evidence = logfile.read_bytes()
            markers = (b'provisional system root active', b'bootstrap root restored',
                       b'[HypeR bootstrap stage 1][fallback-sh] recovery console ready')
            positions = [evidence.index(marker) for marker in markers]
            if positions != sorted(positions):
                raise AssertionError('fallback began before precommit root rollback')
        elif case == 'root-switch-failure':
            # Bootstrap's diagnostic prefix and body are separate writes; a
            # fixture log may appear between them. Recovery/namespace probes
            # below establish the boundary, not the banner's presentation.
            session.observe(rb'cannot prepare system root:', 180)
            session.await_text(rb'\[HypeR bootstrap stage 1\]\[fallback-sh\] recovery console ready', 60)
            session.await_text(rb'fallback-sh\$ ')
            shell(session, b'/bin/recovery-echo ROOT_PREPARATION_RECOVERED', recovery=True)
            shell(session, b'cd /etc/hyper', recovery=True)
            directory = shell(session, b'pwd', recovery=True)
            if b'\n/etc/hyper\n' not in directory:
                raise AssertionError('failed root preparation lost the original namespace')
            if b'provisional system root active' in logfile.read_bytes():
                raise AssertionError('failed root preparation published a new root')
        elif case == 'critical-exit':
            session.observe(re.escape(ADOPTED), 180)
            session.observe(rb'provider adoption failed: bootstrap retirement disabled; /io-runtime:', 30)
            session.observe(CRITICAL_TERMINATED, FORMAL_STARTUP_TIMEOUT)
            formal_prompt()
            # Formal init already owns supervision. The formal session manager
            # remains independent of the failed business service.
            for name in ('session-manager', 'terminal-service', 'console-raw', 'component-manager'):
                processes = shell(session, f'ps --name {name}'.encode())
                if not re.search(name.encode() + rb'\s+running\b', processes):
                    raise AssertionError(f'boot-critical recovery lost {name}: {processes!r}')
            shell(session, b'echo FORMAL_CRITICAL_FAILURE_OK')
        elif case == 'critical-exit-after-ready':
            session.observe(re.escape(ADOPTED), 180)
            session.observe(rb'HypeR system-startup: VM fleet configured', FORMAL_STARTUP_TIMEOUT)
            formal_prompt()
            shell(session, b'echo FORMAL_STARTUP_COMMITTED')
            # The fixture waits for this explicit trigger, so failure cannot
            # race the startup decision merely because the host is slow.
            boundary = session.mark()
            if re.search(CRITICAL_TERMINATED, logfile.read_bytes()):
                raise AssertionError('critical provider exited before the explicit trigger')
            shell(session, b'touch /tmp/critical-exit')
            session.observe(CRITICAL_TERMINATED, 60, after=boundary)
        elif case == 'missing-system-shell':
            session.observe(re.escape(ADOPTED), 180)
            session.observe(rb'HypeR system-startup: VM fleet configured', FORMAL_STARTUP_TIMEOUT)
            for _ in range(3):
                session.await_text(rb'HypeR session-manager: session ended:', 30)
        else:
            if case == 'timeout':
                session.observe(rb'READINESS-FIXTURE-RUNNING')
                # No shell may consume terminal input before boot resolves.
                # The live provider is checked after the real readiness deadline.
                session.pump(1)
                if re.search(rb'(?:hyper|fallback)-sh\$ ', logfile.read_bytes()):
                    raise AssertionError('readiness wait started an early shell')
            # Exercise the production 120 s deadline, not a test-only override.
            session.observe(re.escape(ADOPTED), 240)
            # Live fixtures can consume the full 60 s adoption deadline after
            # supervision transfers. Preserve that startup budget when moving
            # the degraded-state assertion from the serial mirror to journal.
            formal_prompt(FORMAL_STARTUP_TIMEOUT)
            # Splash and kernel output may split the serial mirror mid-line.
            # The attributed journal record is the complete service evidence.
            Journal(session.shell).message('/startup', DEGRADED.decode(), timeout=120)
            if case == 'root-change':
                # The observer can finish before init logs the unavailable
                # fleet. Check the complete capture, not only unread output.
                deadline = time.monotonic() + 60
                while b'ROOT-SWITCH: live capabilities preserved' not in logfile.read_bytes():
                    if time.monotonic() >= deadline:
                        raise AssertionError('live capability observer did not finish')
                    session.pump(0.1)
            shell(session, b'echo NATIVE_RECOVERY_OK')
            config = shell(session, b'cat /boot/etc/hyper/board.json')
            if b'hyper.board.v1' not in config:
                raise AssertionError('initramfs files are unavailable after storage failure')
            memory = shell(session, b'free --bytes')
            if not re.search(rb'Owners:[^\n]*guest=0 B', memory):
                raise AssertionError('failed bootstrap unexpectedly started a guest')
            processes = shell(session, b'ps')
            for name in (b'console-raw', b'session-manager', b'terminal-service',
                         b'component-manager', b'vm-manager'):
                if not re.search(rb'\s' + name + rb'\s+running\b', processes):
                    raise AssertionError(f'base service {name!r} did not survive: {processes!r}')
            listing = shell(session, b'vmm list')
            if re.search(rb'^\S+\s+(?:starting|running|stopping|stopped|failed)\s+', listing, re.M):
                raise AssertionError('unavailable fleet published VM definitions')
            start = session.shell.check('vmm start alpine', failed=True)
            if b'VM fleet unavailable: initial configuration is unavailable' not in start:
                raise AssertionError(f'vmm did not report unavailable configuration: {start!r}')
            deadline = time.monotonic() + (3 if case in LIVE_PROVIDERS else 30)
            while True:
                processes = shell(session, b'ps --name io-runtime')
                live = re.search(rb'io-runtime\s+running\b', processes)
                if case in LIVE_PROVIDERS:
                    # A readiness failure does not prove failed hardware
                    # ownership. Retain live services for recovery after handoff.
                    if not live:
                        raise AssertionError(f'{case}: recovery lost the live provider')
                    if time.monotonic() >= deadline:
                        break
                elif not re.search(rb'io-runtime\s+(?:running|stopping)\b', processes):
                    break
                elif time.monotonic() >= deadline:
                    raise AssertionError(f'{case}: failed provider did not terminate')
                session.pump(0.1)
        evidence = {
            'closed-ready': b'storage unavailable: readiness channel closed',
            'invalid-ready': b'storage unavailable: invalid readiness reply',
            'oversized-ready': b'storage unavailable: invalid readiness reply',
            'timeout': b'storage unavailable: readiness timed out',
            'launch-failure': b"failed to launch service 'io-runtime': service image or resources unavailable",
        }.get(case)
        if evidence is not None:
            session.observe(re.escape(evidence), timeout=30)
        # provider-exit is proved by readiness failure plus the live-process
        # check above. Supervision transfer can race the old supervisor's exit
        # log; that optional diagnostic is not a lifecycle contract.
    output = logfile.read_bytes()
    if case in ('critical-exit', 'critical-exit-after-ready') and DEGRADED in output:
        raise AssertionError('critical service failure was swallowed as optional storage failure')
    if case not in ('handoff-failure', 'root-switch-failure') and b'[HypeR bootstrap stage 1][fallback-sh] recovery console ready' in output:
        raise AssertionError('a post-handoff failure reopened bootstrap recovery')
    if case == 'missing-config' and b'cannot open VM fleet configuration' not in output:
        raise AssertionError('missing-config scenario failed before opening the fleet file')
    # NOT_FOUND from the real controller claim must be fully queued before
    # READY closes. Init's emergency diagnostics may interleave with the line.
    if case == 'missing-controller' and b'Status(Status(-16))' not in output:
        raise AssertionError('provider failure diagnostic lost its controller error cause')
    expected_handoffs = 0 if case in ('handoff-failure', 'root-switch-failure') else 1
    if output.count(ADOPTED) != expected_handoffs:
        raise AssertionError(f'{case}: unexpected system-init handoff count')
    if case == 'handoff-failure' and b'HypeR session: console ready' in output:
        raise AssertionError('bootstrap recovery unexpectedly started the formal shell')
    if b'HypeR: vCPU 0 running as scheduler thread' in output:
        raise AssertionError('guest started after failed bootstrap')
    print(f'verified storage failure case: {case}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='action', required=True)
    prepare_parser = subparsers.add_parser('prepare')
    for name in ('sdk', 'apps'):
        prepare_parser.add_argument('--' + name, type=Path, required=True)
    prepare_parser.add_argument('--boot-apps', type=Path)
    prepare_parser.add_argument('--fixture-init', type=Path, required=True)
    prepare_parser.add_argument('--io-image', type=Path, required=True)
    prepare_parser.add_argument('--boot-components', nargs='+', required=True)
    for name in ('packer', 'strip'):
        prepare_parser.add_argument('--' + name, required=True)
    prepare_parser.add_argument('--output', type=Path, required=True)
    prepare_parser.add_argument('--case', choices=CASES)
    run_parser = subparsers.add_parser('run')
    run_parser.add_argument('--qemu', required=True)
    run_parser.add_argument('--image', type=Path, required=True)
    run_parser.add_argument('--output', type=Path, required=True)
    run_parser.add_argument('--case', choices=CASES)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args)
    else:
        for case in (args.case,) if args.case else CASES:
            run_case(args, case)


if __name__ == '__main__':
    main()
