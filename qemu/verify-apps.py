#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Exercise Native file tools and independent named VM lifecycles."""
import os
from pathlib import Path
import re
import sys
import time

from hyper_test.session import Session, native_command
from handle_checks import verify_handles
from command_checks import verify_command_options
from hyper_test.command import Shell


def main():
    qemu, image, initramfs, logfile = sys.argv[1:]
    verify_vm = os.environ.get("HYPER_TEST_VM", "1")
    if verify_vm not in ("0", "1"):
        raise ValueError("HYPER_TEST_VM must be 0 or 1")
    verify_vm = verify_vm == "1"
    command = native_command(qemu, image, initramfs)
    with Session(command, logfile) as session:
        await_text = session.await_text
        send = session.send

        shell = Shell(session)

        def run(text, expected=None, failed=False, timeout=60):
            output = shell.check(text, failed=failed, timeout=timeout)
            if expected is not None and not re.search(expected, output):
                raise AssertionError(f'{text}: missing {expected!r}: {output!r}')
            return output

        def state(name, wanted, timeout=60):
            # Bound elapsed time, not the number of fast status commands.
            # Guest setup on a loaded CI runner can outlive 50 short polls.
            deadline = time.monotonic() + timeout
            output = b''
            while (remaining := deadline - time.monotonic()) > 0:
                output = run(f'vmm status {name}', timeout=remaining)
                if re.search(name.encode() + rb'\s+' + wanted.encode() + rb'\s', output):
                    return
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            raise AssertionError(f'{name} did not reach {wanted}: {output!r}')

        # Other services may log before the shell prints its first prompt.
        await_text(rb'HypeR session: console ready\n')
        await_text(rb'hyper-sh\$ ')
        if (Path(__file__).resolve().parents[2] / 'stdlib/musl/component.json').is_file():
            run('/bin/c-standard-test', rb'MUSL-NATIVE: PASS\n')
            run('/bin/c-standard-test assert', rb'Assertion failed:.*standard C assertion probe.*test.c:[0-9]+, main', failed=True)
            run('/bin/cxx-standard-test', rb'LIBCXX-NATIVE: PASS\n')
        if verify_vm:
            state('alpine', 'running')
        run('ls -d /lib', rb'l[rwx-]{9}\s+.* /lib@ -> lib64\n')
        architecture = re.search(rb'\n((?:aarch64|riscv64)-hyper-hyper)/\n', run('ls -1 /lib/'))
        if architecture is None:
            raise AssertionError('missing architecture-specific library directory')
        for prefix in ('/lib/', '/lib64/'):
            run(f'ls -1 {prefix}{architecture[1].decode()}', rb'\nlibhyper_rust_std.so\n')
        # Pipelines run concurrently, close unused endpoints, and preserve
        # file-open/truncation semantics without buffering whole commands.
        run('echo Alpha> /pipe-data')
        run('echo beta>> /pipe-data')
        run('cat</pipe-data|grep -in alpha', rb'\n1:Alpha\n')
        run('cat /pipe-data|grep -v Alpha|grep -c beta', rb'\n1\n')
        run('grep -n "^beta$" /pipe-data', rb'\n2:beta\n')
        run('grep missing /pipe-data', failed=True)
        run('grep "[" /pipe-data', rb'grep:', failed=True)
        run('cat /missing 2>/pipe-errors', failed=True)
        run('grep cat: /pipe-errors', rb'cat:')
        run('cat /missing 2>>/pipe-errors', failed=True)
        run('grep -c cat: /pipe-errors', rb'\n2\n')
        run('pwd|grep /', rb'\n/\n')
        run('pwd>/pipe-pwd')
        run('cat /pipe-pwd', rb'\n/\n')
        run('echo "a|b>c"|grep -F "a|b>c"', rb'\na\|b>c\n')
        run('echo ignored>/pipe-data|cat')
        run('cat /pipe-data', rb'\nignored\n')
        # Redirecting stdin closes the pipe reader; upstream may report EPIPE.
        # Keep that diagnostic separate from the consumer output being checked.
        run('echo upstream 2>/pipe-upstream-errors|cat</pipe-pwd', rb'\n/\n')
        # Input exceeds channel capacity; -q closes early and the producer
        # must observe peer closure instead of blocking the shell forever.
        run('cat /bin/grep|grep -q .', timeout=15)
        run('cat /bin/grep|cat|cat>/pipe-copy', timeout=30)
        sizes = run('ls --bytes /bin/grep /pipe-copy')
        original = re.search(rb'(-[rwx-]{9})\s+(\d+)\s+/bin/grep', sizes)
        copied = re.search(rb'(-[rwx-]{9})\s+(\d+)\s+/pipe-copy', sizes)
        if not original or not copied or original[2] != copied[2]:
            raise AssertionError(f'pipeline copy truncated: {sizes!r}')
        # Malformed commands and failed preparation must leave a usable shell.
        run('echo upstream|/missing', rb'command not found', failed=True)
        run("sh -n -c 'echo ignored |'", rb'expected a command', failed=True)
        run('cat </missing', rb'/missing:', failed=True)
        run('mkdir /pipe-dir')
        run('cd /pipe-dir')
        run('echo relative>data')
        run('pwd|grep "^/pipe-dir$"', rb'\n/pipe-dir\n')
        run('cat<data|grep relative', rb'\nrelative\n')
        run('cd /')
        run('echo PIPE_RECOVERED', rb'\nPIPE_RECOVERED\n')
        run('cat /etc/hyper/vms.json', rb'"format": "hyper.vm-config"')
        run('cat -n /etc/hyper/vms.json', rb'\n\s+1\t\{')
        run('cat /missing /etc/hyper/vms.json', rb'"virtual-machines"', failed=True)
        run('ls --bytes /bin/cat /etc/hyper/vms.json', rb'-rwxr-xr-x\s+\d+\s+/bin/cat')
        run('ls -1 /bin', rb'\ncat\n')
        run('ls /missing /etc/hyper', rb'vms.json', failed=True)
        for tool in ('mv', 'ln', 'rm', 'chmod', 'cp', 'mkdir', 'rmdir', 'touch'):
            run(f'{tool} --help', rb'Usage:')
        run('mkdir -p -m 750 /file-tools/source/sub /file-tools/outside')
        run('ls /file-tools/source', rb'drwxr-x---\s+-\s+sub/')
        run('cp /etc/hyper/vms.json /file-tools/source/data')
        run('cp /file-tools/source/data /file-tools/source/data', rb'same file', failed=True)
        run('ln /file-tools/source/data /file-tools/hard')
        run('cp /file-tools/source/data /file-tools/hard', rb'same file', failed=True)
        run('cat /file-tools/hard', rb'"format": "hyper.vm-config"')
        run('ln -s data /file-tools/source/relative')
        run('ln -s absent /file-tools/source/dangling')
        run('ln -s /file-tools/outside /file-tools/source/external')
        links = run('ls /file-tools/source', rb'\brelative@ -> data\n')
        for target in (rb'\bdangling@ -> absent\n', rb'\bexternal@ -> /file-tools/outside\n'):
            if not re.search(target, links):
                raise AssertionError(f'ls omitted a symlink target: {links!r}')
        run('ls /file-tools/source/dangling', rb'/file-tools/source/dangling@ -> absent\n')
        run('ls -1 /file-tools/source/dangling', rb'\n/file-tools/source/dangling@\n')
        run('touch /file-tools/outside/keep')
        run('cp -R /file-tools/source /file-tools/copy')
        run('ls -1 /file-tools/copy', rb'\nrelative@\n')
        run('cat /file-tools/copy/relative', rb'"format": "hyper.vm-config"')
        run('cp -R /file-tools/source /file-tools/source/child', rb'into itself', failed=True)
        run('ln -s /file-tools/source/sub /file-tools/alias')
        run('cp -R /file-tools/source /file-tools/alias/child', rb'into itself', failed=True)
        run('mv /file-tools/copy/data /file-tools/copy/renamed')
        run('cat /file-tools/copy/data', failed=True)
        run('cat /file-tools/copy/renamed', rb'"format": "hyper.vm-config"')
        run('chmod 600 /file-tools/copy/renamed')
        run('ls /file-tools/copy/renamed', rb'-rw-------\s+.*renamed')
        run('chmod u+x,go+r /file-tools/copy/renamed')
        run('ls /file-tools/copy/renamed', rb'-rwxr--r--\s+.*renamed')
        run('chmod -R u+rwX,go-rwx /file-tools/copy')
        run('ls /file-tools/copy', rb'drwx------\s+-\s+sub/')
        run('touch -r /etc/hyper/vms.json /file-tools/copy/renamed /file-tools/copy')
        run('cat /file-tools/copy/renamed', rb'"format": "hyper.vm-config"')
        run('touch -c /file-tools/absent')
        run('cat /file-tools/absent', failed=True)
        run('mkdir /file-tools/empty')
        run('rmdir /file-tools/empty')
        run('rmdir /file-tools/source', failed=True)
        run('rm /file-tools/source', failed=True)
        run('rm -rf /', rb'refusing', failed=True)
        run('rm -rf /file-tools/source/.', rb'refusing', failed=True)
        run('ln -s /file-tools/outside /file-tools/unlink-only')
        run('rm -r /file-tools/unlink-only/')
        run('mkdir /file-tools/batch')
        run('cp -R /file-tools/source/sub/. /file-tools/batch')
        run('touch /file-tools/one /file-tools/two')
        run('mv /file-tools/one /file-tools/two /file-tools/batch')
        run('cp /file-tools/batch/one /file-tools/batch/two /file-tools/outside')
        run('ls -1 /file-tools/outside', rb'\none\ntwo\n')
        run('rm -r /file-tools/copy /file-tools/source')
        run('ls /file-tools/outside/keep', rb'keep')
        run('cat /file-tools/hard', rb'"format": "hyper.vm-config"')
        run('rm -f /file-tools/absent')
        run('rm -r /file-tools')
        run('ls /file-tools', failed=True)
        run('top -b -n 1 -d 0.1', rb'CPU: user-thread')
        run('free --bytes', rb'Mem:\s+\d+ B')
        run('ps --name shell', rb'process\s+0x[0-9a-f]{16}.*shell')
        run('ps -T --name shell', rb'thread\s+0x[0-9a-f]{16}\s+0x[0-9a-f]{16}\s+shell\s+user/resident')
        verify_handles(run)
        verify_command_options(run)
        if verify_vm:
            # This archive grants no I/O broker capability. A name alone must
            # not fabricate an entry or determine its access permissions.
            for action in ('status', 'start', 'stop', 'restart', 'delete', 'console'):
                run(f'vmm {action} io', rb"VM 'io' does not exist", failed=True)
            run('vmm affinity io 0 0', rb"VM 'io' does not exist", failed=True)
            run('vmm status missing', rb"does not exist", failed=True)
            run('vmm create io --config /etc/hyper/vms.json --from missing', rb'not found in configuration', failed=True)
            missing_image = '{"format":"hyper.vm-config","virtual-machines":[{"name":"io","image":"/missing","configuration":{"vcpus":1,"memory-bytes":67108864,"bootargs":""}}]}'
            run(f"echo '{missing_image}' > /missing-vm.json")
            run('vmm create io --config /missing-vm.json', rb'cannot open image', failed=True)
            run('rm /missing-vm.json')
            output = run('vmm list')
            assert not re.search(rb'\nio\s', output)
            run('vmm create io --config /etc/hyper/vms.json --from alpine', rb'accepted')
            run('vmm create io --config /etc/hyper/vms.json --from alpine', rb'already exists', failed=True)
            run('vmm start io --wait --timeout 60', rb'io: running', timeout=65)
            state('io', 'running')
            state('alpine', 'running')
            run('vmm affinity io 0 0', rb'vCPU 0: affinity accepted')
            run('vmm delete io', rb'stop the VM', failed=True)
            send(b'vmm console io\n')
            await_text(rb'Connected to io\.')
            # Validate the guest launched through the CLI, not a kernel boot
            # shortcut or another VM's retained output.
            await_text(rb'HypeR guest: Linux userspace is running')
            await_text(rb'~ # ')
            send(b'\x1b[1;1Recho HYPER_CLI_GUEST_OK\n')
            await_text(rb'\nHYPER_CLI_GUEST_OK\n')
            send(b'\x1dq')
            # Kernel retirement diagnostics may arrive between detachment and
            # the next prompt; both events must occur, but need not be adjacent.
            await_text(rb'\[vmm\] detached\n')
            await_text(rb'hyper-sh\$ ')
            run('vmm stop io --wait --timeout 60', rb'io: stopped', timeout=65)
            state('io', 'stopped')
            state('alpine', 'running')
            run('vmm delete io', rb'accepted')
            assert not re.search(rb'\nio\s', run('vmm list'))
            run('vmm stop alpine', rb'accepted')
            state('alpine', 'stopped')
            run('vmm delete alpine', rb'accepted')
            assert b'alpine' not in run('vmm list')
            run('vmm create alpine --config /etc/hyper/vms.json --start', rb'accepted')
            state('alpine', 'running')
            run('vmm restart alpine --wait --timeout 60', rb'alpine: running', timeout=65)
            state('alpine', 'running')
        run('echo HYPER_APPS_OK', rb'\nHYPER_APPS_OK\n')
        print('verified Native file tools' +
              (', named VM isolation, and create/delete/restart' if verify_vm else ''))


if __name__ == '__main__':
    main()
