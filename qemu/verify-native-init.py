#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Native boot acceptance: explicit phases, command receipts and scoped evidence."""
from functools import partial
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from hyper_test.command import Shell
from hyper_test.guest_console import append_console_output
from hyper_test.journal import Journal
from hyper_test.session import Session, native_command


TOP_SUMMARY = (rb'top - ([0-9]+) CPUs\nCPU: user-thread ([0-9]+)\.([0-9]{2})%'
               rb' kthread ([0-9]+)\.([0-9]{2})% vcpu ([0-9]+)\.([0-9]{2})%'
               rb' idle ([0-9]+)\.([0-9]{2})%\n')


def verify_top_summary(output, cpus):
    """Require a populated elapsed-runtime sample, including every CPU category."""
    match = re.search(TOP_SUMMARY, output)
    if match is None or int(match[1]) != cpus:
        raise AssertionError(f'top CPU summary is missing or has the wrong CPU count: {output!r}')
    shares = [int(match[index]) * 100 + int(match[index + 1])
              for index in (2, 4, 6, 8)]
    # top truncates four percentages independently to hundredths. A live sample
    # must account for the whole interval, with at most three lost basis points.
    if any(share > 10_000 for share in shares) or not 9_997 <= sum(shares) <= 10_000:
        raise AssertionError(f'top CPU sample is empty or inconsistent: {shares!r}')


def main():
    qemu, image, initramfs, cpu, cpus, memory, bootargs = sys.argv[1:]
    verify_vm = os.environ.get('HYPER_TEST_VM', '1')
    if verify_vm not in ('0', '1'):
        raise ValueError('HYPER_TEST_VM must be 0 or 1')
    os.environ.update(QEMU_CPU=cpu, QEMU_CPUS=cpus, QEMU_MEMORY=memory, QEMU_BOOTARGS=bootargs)
    phase_timeout = float(os.environ.get('QEMU_BOOT_TIMEOUT_SECONDS', '90'))
    total_timeout = float(os.environ.get('QEMU_NATIVE_TIMEOUT_SECONDS', '300'))
    started = time.monotonic()
    log = Path(os.environ['QEMU_TEST_LOG']) if 'QEMU_TEST_LOG' in os.environ else (
        Path(tempfile.mkdtemp(prefix='hyper-native-')) / 'serial.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    failures = (b'HypeR: fatal', b'HypeR KERNEL PANIC', b'HypeR crash monitor',
                b'kernel panic', b'Kernel panic', b'kernel startup failed',
                b'bootstrap failed', b'fatal after ownership commit', b'critical service ',
                b'VM fleet configuration rejected', b'VM fleet result channel closed',
                b'VM fleet configuration reply is invalid', b'VM provisioning channel closed')
    # Guest printk can split even command echo. Preserve it in the raw log,
    # but reassemble terminal bytes before matching interactive responses.
    with Session(native_command(qemu, image, initramfs), log, failures=failures,
                 output_filter=partial(append_console_output, filter_guest_logs=True)) as vm:
        shell = Shell(vm)
        journal = Journal(shell)

        def budget():
            remaining = total_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError('Native acceptance overall deadline expired')
            return min(phase_timeout, remaining)

        def wait(pattern):
            return vm.await_text(pattern, timeout=budget())

        def run(command, *patterns, failed=False):
            print(f'Native acceptance: {command} (+{time.monotonic() - started:.2f}s)', flush=True)
            output = shell.check(command, failed=failed, timeout=budget())
            for pattern in patterns:
                if not re.search(pattern, output, re.M):
                    raise AssertionError(f'{command}: missing {pattern!r}: {output!r}')
            return output

        wait(rb'HypeR session: console ready\n')
        wait(rb'hyper-sh\$ ')
        # Actual line-editor behavior stays on the raw console, not the report path.
        vm.send(b'\r\n\r\n\x7f\x7f\x08vmm --help\r')
        wait(rb'Usage: vmm')
        wait(rb'hyper-sh\$ ')
        if verify_vm == '1':
            deadline = time.monotonic() + budget()
            while True:
                state = run('vmm status alpine')
                if re.search(rb'\nalpine\s+running\s', state) and b'allocated VM backing:' in state:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f'alpine did not start: {state!r}')
                vm.pump(0.1)
            vm.send(b'vmm console alpine\n')
            wait(rb'HypeR guest: Linux userspace is running')
            wait(rb'~ # ')
            vm.send(b'echo HYPER_GUEST_CONSOLE_RX')
            wait(rb'echo HYPER_GUEST_CONSOLE_RX')
            vm.send(b'\r')
            wait(rb'\nHYPER_GUEST_CONSOLE_RX\n')
            vm.send(b'\x1dd')
            wait(rb'\[vmm\] detached')
            wait(rb'hyper-sh\$ ')
            for component, message in (('/vm-manager', 'HypeR vm-manager: ready'),
                                       ('/startup', 'VM fleet configured')):
                journal.message(component, message, timeout=budget())

        processes = run('ps', rb'^TYPE\s+KOID\s+OWNER\s+NAME\s+STATE$',
                        rb'^process\s+0x[0-9a-f]+\s+-\s+\S+\s+(created|running|stopping|stopped|retiring|retired)')
        process = re.search(rb'^process\s+(0x[0-9a-f]+)\s+-\s+shell\s', processes, re.M)
        if process is None:
            raise AssertionError('persistent shell Process missing')
        threads = run('ps --threads', rb'^  thread\s+0x[0-9a-f]+\s+0x[0-9a-f]+\s+\S+\s+user/(resident|retiring) process=',
            rb'^  thread\s+0x[0-9a-f]+\s+kernel\s+\S+\s+(bootstrap|idle|kernel|vcpu)/(resident|retiring) process=kernel',
            rb'\skernel\s+idle/0\s+idle/resident')
        for index in range(int(cpus)):
            for name, role in (('idle', 'idle'), ('ktimer', 'kernel')):
                pattern = (rb'^  thread\s+0x[0-9a-f]+\s+kernel\s+'
                           + f'{name}/{index}'.encode() + rb'\s+'
                           + role.encode() + rb'/resident\s+process=kernel')
                if len(re.findall(pattern, threads, re.M)) != 1:
                    raise AssertionError(f'missing or duplicate {name}/{index}: {threads!r}')
        for process_name, names in (
                ('session-manager', ('terminal-recovery', 'thread-reaper')),
                ('journal', ('journal-admit', 'journal-query-admit', 'thread-reaper')),
                ('console-raw', ('console-control', 'thread-reaper'))):
            for name in names:
                pattern = (rb'^  thread\s+0x[0-9a-f]+\s+0x[0-9a-f]+\s+'
                           + name.encode() + rb'\s+user/resident process='
                           + process_name.encode() + rb'$')
                if not re.search(pattern, threads, re.M):
                    raise AssertionError(f'missing {process_name}/{name}: {threads!r}')
        run('handle ' + process[1].decode(), rb'^HANDLE\s+OBJECT\s+KIND\s+RIGHTS$',
            rb'^0x[0-9a-f]{16}\s+0x[0-9a-f]{16}\s+[a-z][a-z-]+\s+[a-z][a-z|-]+')
        run('handle --objects', rb'^KOID\s+KIND\s+STATE\s+HANDLES\s+REFS$',
            rb'^0x[0-9a-f]{16}\s+[a-z][a-z-]+\s+(unpublished|active|retired)')
        run('dynamic-test', rb'^HYPER_DYNAMIC_LINK_OK$')
        run('echo-static HYPER_STATIC_LINK_OK', rb'^HYPER_STATIC_LINK_OK$')
        # Terminal input semantics have dedicated console/terminal scenarios;
        # this probe checks std input and captures both output streams to EOF.
        dynamic = run('std-test --name dynamic --read-input <<<abcdef', rb'^HYPER_STD_INPUT_READY$')
        static = run('std-test-static --name static', rb'^HYPER_STD_OK hello static$')
        for name, output in ((b'dynamic', dynamic), (b'static', static)):
            for marker in (b'HYPER_STD_OK hello ' + name, b'HYPER_STD_TLS_DROP_OK',
                           b'HYPER_STD_FP_THREADS_OK', b'HYPER_COW_OK', b'HYPER_STD_STDERR_OK',
                           b'HYPER_STD_THREAD_NAMES_OK', b'HYPER_STD_SCHEDULING_OK'):
                if output.count(marker) != 1:
                    raise AssertionError(f'std {name!r}: missing/duplicate {marker!r}: {output!r}')
        run('std-test --help', rb'^HypeR standard library acceptance probe$', rb'^Usage: .*std-test')
        run('std-test --unknown-option', rb"unexpected argument '--unknown-option'", failed=True)
        run('std-test --panic', rb'HYPER_STD_EXPECTED_PANIC', failed=True)
        run('help', rb'Builtins: cd pwd echo printf')
        run('echo HYPER_SHELL_BUILTIN_OK', rb'^HYPER_SHELL_BUILTIN_OK$')
        run('ls -1', rb'^boot/$', rb'^bin/$', rb'^data/$', rb'^tmp/$')
        run('cd /bin; pwd; ls -1', rb'^/bin$', rb'^echo$', rb'^ls$', rb'^vmm$')
        run('./echo HYPER_CD_CHILD_OK', rb'^HYPER_CD_CHILD_OK$')
        run('cd ..; pwd; bin/echo HYPER_CD_PARENT_OK', rb'^/$', rb'^HYPER_CD_PARENT_OK$')
        value = rb'[0-9]+(?:\.[0-9])? (?:B|KiB|MiB|GiB|TiB|PiB|EiB)'
        maybe = rb'(?:' + value + b'|' + '—'.encode() + rb')'
        run('free', rb'^Mem:\s+' + value + rb'\s+' + maybe + rb'\s+' + value + rb'(?:\s+' + maybe + rb'){2}$',
            rb'^Reserved: ' + value + rb'  Managed: ' + value + rb'  Allocated: ' + value + rb'$',
            rb'^Owners:\s+kernel=' + maybe + rb' user=' + value + rb' guest=' + value
            + rb' buffered/cached=' + maybe + rb' other=' + value + rb'$')
        # A 60-second refresh delay must never delay the first (or only) frame.
        output = shell.check('top -b -n 1 -d 60', timeout=min(10, budget()))
        verify_top_summary(output, int(cpus))
        if output.count(b'top - ') != 1 or b'since boot' not in output or b'\x1b' in output:
            raise AssertionError(f'invalid immediate batch frame: {output!r}')
        output = shell.check('top -b -n 2 -d 0.1 -H', timeout=min(10, budget()))
        if output.count(b'top - ') != 2 or b'interval' not in output or b'TID (KOID)' not in output:
            raise AssertionError(f'invalid repeated thread frames: {output!r}')

        def top_frame(pattern):
            # Resize/control notifications may redraw an older retained frame.
            # Keep one deadline while waiting for the requested view's footer.
            deadline = time.monotonic() + min(10, budget())
            while time.monotonic() < deadline:
                output = vm.await_text(rb'h help q quit', timeout=deadline - time.monotonic())
                if re.search(pattern, output):
                    return
            raise AssertionError(f'missing top frame {pattern!r}')

        vm.send(b'top -d 60\n')
        top_frame(rb'CPU: user-thread')
        vm.send(b'H')
        top_frame(rb'TID \(KOID\)')
        vm.send(b'I')
        top_frame(rb'task CPU: whole machine')
        vm.send(b'T')
        top_frame(rb'Time \|')
        vm.send(b'h')
        top_frame(rb'interactive help')
        vm.send(b' ')  # dismiss help without taking a new sample
        top_frame(rb'since boot')
        vm.send(b' ')  # explicitly sample now, without waiting 60 seconds
        top_frame(rb'interval')
        vm.send(b'd')
        vm.await_text(rb'Delay seconds', timeout=min(10, budget()))
        vm.send(b'0.5\r')
        top_frame(rb'delay 0.5s')
        vm.send(b'q')
        wait(rb'hyper-sh\$ ')
        run('echo HYPER_NATIVE_ECHO_OK', rb'^HYPER_NATIVE_ECHO_OK$')
        for marker in (b'HypeR: starting Native init process', b'provisional system root active; preparing successor'):
            vm.observe(re.escape(marker), timeout=budget())
        journal.message('/startup', 'core supervision adopted', timeout=budget())
    print(f'verified Native boot, services, dynamic/static std and tools: {log}')


if __name__ == '__main__':
    main()
