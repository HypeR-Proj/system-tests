# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""User-facing command options exercised through Native processes and the VFS."""

import re
import json


def verify_command_options(run):
    verify_memory_statistics(run)
    dependencies = run('ldd /bin/vmm', rb'libhyper_vm_policy_shared.so => /lib64/')
    if dependencies.count(b'libhyper_rust_std.so =>') != 1:
        raise AssertionError(f'ldd did not deduplicate transitive dependencies: {dependencies!r}')
    run('ldd --tree /svc/vm-runtime', rb'libhyper_vm_support_shared.so =>')
    direct = run('ldd --direct /bin/vmm', rb'\(loader\)')
    names = re.findall(rb'\n    (lib\S+) =>', direct)
    if sorted(names) != sorted([b'libhyper_vm_policy_shared.so', b'libhyper_clap_shared.so',
                               b'libhyper_rust_std.so', b'libsystem.so']):
        raise AssertionError(f'ldd --direct did not report the ELF dependency list: {direct!r}')
    run('ldd /bin/echo', rb'libhyper_clap_shared.so => /lib64/')
    run('ldd -v /bin/vmm', rb'ELF64 (aarch64|riscv64), OS ABI 0x3f')
    run('ldd /bin/echo-static', rb'statically linked')
    run('ldd /etc/hyper/vms.json /bin/echo-static', rb'statically linked', failed=True)
    run('mkdir /missing-libraries')
    run('ldd --library-dir /missing-libraries /bin/vmm', rb'libsystem.so => not found', failed=True)
    run('rmdir /missing-libraries')
    run('mkdir -v /tool-options', rb'created directory /tool-options')
    run("echo -e 'one\\n\\ntwo\\n\\n\\nthree' > /tool-options/text")
    run('cat -bs /tool-options/text', rb'1\tone\n\n\s+2\ttwo\n\n\s+3\tthree\n')
    run('grep -xm 1 two /tool-options/text', rb'\ntwo\n')
    run('grep -cm 0 . /tool-options/text', rb'\n0\n', failed=True)
    run('echo -n short > /tool-options/short')
    run('ls --bytes /tool-options/short', rb'\s5\s+/tool-options/short')
    run('cp -nv /tool-options/text /tool-options/short')
    run('ls --bytes /tool-options/short', rb'\s5\s+/tool-options/short')
    run('cp -v /tool-options/text /tool-options/copy', rb'text -> /tool-options/copy')
    run('mkdir /tool-options/dest')
    run('cp -T /tool-options/text /tool-options/dest', failed=True)
    run('mv -vT /tool-options/copy /tool-options/renamed', rb'copy -> /tool-options/renamed')
    run('chmod -v 600 /tool-options/renamed', rb'-> 0600')
    run('ln -sv dest /tool-options/link', rb'link -> dest')
    run('ln -sn file /tool-options/link', failed=True)
    run('ls -d /tool-options/dest', rb'/tool-options/dest/')
    run('touch -d @100 /tool-options/text')
    run('touch -d @200 /tool-options/short')
    listing = run('ls -1t /tool-options')
    if listing.index(b'\nshort\n') > listing.index(b'\ntext\n'):
        raise AssertionError(f'ls -t did not put newer files first: {listing!r}')
    run('mkdir -p /tool-options/remove/a/b')
    run('cd /tool-options')
    run('rmdir -pv remove/a/b', rb'removed directory remove\n')
    run('cd /')
    run('rm -dv /tool-options/dest', rb'removed /tool-options/dest')
    run('rm -d /tool-options', failed=True)
    run('rm -r /tool-options')

    processes = run('ps --name shell --no-headers')
    row = re.search(rb'\nprocess\s+(0x[0-9a-f]{16})\s+-\s+shell\s', processes)
    if not row or b'TYPE ' in processes:
        raise AssertionError(f'invalid ps rows: {processes!r}')
    koid = row[1].decode()
    run(f'ps -p {koid} -T', rb'thread\s+0x[0-9a-f]{16}')
    run(f'top -b -n 1 -d 0.1 -p {koid} --sort name --limit 1', row[1] + rb'\s+[\d.]+%\s+\d+:\d{2}\.\d{2}\s+\d+\s+running\s+shell')
    samples = run('free -m -c 2 -s 0.1')
    if len(re.findall(rb'\nMem:.*MiB', samples)) != 2:
        raise AssertionError(f'free did not report two MiB samples: {samples!r}')
    run('handle shell --summary --kind directory --no-headers', rb'\ndirectory\s+[1-9]\d*\s+[1-9]\d*\n')
    run('handle --summary --kind process', rb'\nprocess\s+[1-9]\d*\n')


def verify_memory_statistics(run):
    """Check the shipped diagnostic ABI and persistence without timing thresholds."""
    run('memstat --save /tmp/memory-before.json', rb'owner.page_tables.resident_pages')
    run('memstat --save /tmp/memory-before.json', rb'memstat:', failed=True)
    run('memstat --diff /tmp/memory-before.json --classes', rb'CHANGE')
    output = run('memstat --json')
    begin = output.find(b'{')
    if begin < 0:
        raise AssertionError(f'no memory statistics JSON: {output!r}')
    sample, _ = json.JSONDecoder().raw_decode(output[begin:].decode())
    if sample['version'] != 1 or sample['started_at_ns'] > sample['finished_at_ns']:
        raise AssertionError(f'invalid memory statistics envelope: {sample!r}')
    metrics = sample['metrics']
    for key in ('heap.live_objects', 'heap.allocations', 'file_cache.index_capacity',
                'buddy.order_18.free_blocks', 'owner.page_tables.resident_pages'):
        if key not in metrics:
            raise AssertionError(f'missing diagnostic metric: {key}')
    classes = [key for key in metrics if key.startswith('heap_class.')]
    if bool(classes) != sample['heap_profiling']:
        raise AssertionError('heap class availability disagrees with configuration')
    run('echo invalid > /tmp/memory-invalid.json')
    run('memstat --diff /tmp/memory-invalid.json', rb'memstat:', failed=True)
    run('rm /tmp/memory-before.json /tmp/memory-invalid.json')
