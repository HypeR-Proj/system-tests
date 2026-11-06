#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Build the manual Pi 5 storage qualification payload and optional SD image."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'test-framework/python'))
from hyper_test.provenance import source_inventory
SOURCE = Path(__file__).resolve().parent
PLATFORM = ROOT / 'tests/performance'


def trace_board(board, firmware_mib):
    """Expose the existing firmware FAT volume only in the opt-in trace image."""
    sys.path.insert(0, str(ROOT / 'scripts'))
    from native_deployment import prepare
    board = prepare(board)
    boot = next(part for part in board['disk']['partitions'] if part.get('content') == 'boot')
    boot['size-mib'] = firmware_mib
    source = 'PARTLABEL=' + boot['label']
    board['native']['volumes'].append({'source': source, 'read-only': False})
    board['native']['fstab'] += f'{source} /tmp/firmware fat rw,noauto 0 0\n'
    return board


def trace_config(text):
    symbol = 'CONFIG_FEATURE_KERNEL_TRACE'
    lines = [line for line in text.splitlines()
             if not line.startswith(symbol + '=') and line != f'# {symbol} is not set']
    return '\n'.join([*lines, symbol + '=y', ''])


def guest_rootfs(archive, program, output):
    """Add the Linux payload without modifying the cached Alpine rootfs."""
    destination = 'usr/bin/storage-qual'
    with tarfile.open(archive) as source, tarfile.open(output, 'w') as target:
        for item in source:
            if item.name.removeprefix('./') == destination:
                continue
            if item.isfile():
                with source.extractfile(item) as contents:
                    target.addfile(item, contents)
            else:
                target.addfile(item)
        item = tarfile.TarInfo(destination)
        item.mode = 0o755
        item.uid = item.gid = item.mtime = 0
        item.size = program.stat().st_size
        with program.open('rb') as contents:
            target.addfile(item, contents)


def main():
    if any(os.environ.get(key, '').lower() in ('1', 'true') for key in ('CI', 'GITHUB_ACTIONS')):
        raise SystemExit('Hardware qualification is local/manual only.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('HYPER_BUILD_OUTPUT', ROOT / 'out')) / 'config/aarch64/modules/kernel.config')
    erofs_tool = ROOT / 'target/host-tools/erofs/bin/mkfs.erofs'
    parser.add_argument('--mkfs-erofs', default=os.environ.get(
        'EROFS_MKFS', str(erofs_tool) if erofs_tool.is_file() else 'mkfs.erofs'))
    parser.add_argument('--payload-only', action='store_true', help='use an existing installed SDK')
    parser.add_argument('--trace', action='store_true',
                        help='enable kernel tracing and package the firmware-FAT export script')
    parser.add_argument('--firmware-mib', type=int, default=512,
                        help='firmware FAT size for --trace, 256..4096 MiB (default: 512)')
    parser.add_argument('--retirement-probe', action='store_true',
                        help='DESTRUCTIVE image: terminate the I/O runtime 60 seconds after readiness')
    args = parser.parse_args()
    out = args.output.resolve()
    if args.payload_only and (args.retirement_probe or args.trace):
        parser.error('--retirement-probe and --trace require an image build')
    if args.trace and args.retirement_probe:
        parser.error('--trace cannot be combined with destructive retirement injection')
    if not 256 <= args.firmware_mib <= 4096:
        parser.error('--firmware-mib must be 256..4096')
    if not os.environ.get('HYPER_BUILD_OUTPUT'):
        command = [sys.executable, ROOT / 'project.py', 'exec', '--', sys.executable,
                   'tests/hardware/storage/prepare.py', '--output', out,
                   '--config', args.config.resolve(), '--mkfs-erofs', args.mkfs_erofs,
                   '--firmware-mib', str(args.firmware_mib)]
        command += [flag for enabled, flag in [(args.trace, '--trace'),
                    (args.retirement_probe, '--retirement-probe'), (args.payload_only, '--payload-only')]
                    if enabled]
        subprocess.run([str(item) for item in command], cwd=ROOT, check=True)
        return
    if any(character in str(out) for character in '\n\r\t\"\'$`\\'):
        parser.error('output path contains unsupported Make/shell metacharacters')
    out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, CARGO_INCREMENTAL='0')
    commands = []

    def run(command, **kwargs):
        command = [str(item) for item in command]
        # Use the complete default product, not a developer's saved selection.
        # This also keeps the kernel output path below independent of product IDs.
        if command[0] == 'make':
            command.append('SYSTEM_CONFIG=')
        commands.append(command)
        print('+', ' '.join(command), flush=True)
        return subprocess.run(command, cwd=ROOT, env=env, check=True, **kwargs)

    clang = shutil.which('clang')
    homebrew_clang = Path('/opt/homebrew/opt/llvm/bin/clang')
    if homebrew_clang.exists():
        clang = str(homebrew_clang)
    if not clang:
        raise SystemExit('Clang is required')
    linker = run(['sh', 'scripts/find-llvm-tool.sh', 'ld.lld'], capture_output=True, text=True).stdout.strip()
    sdk = Path(os.environ['HYPER_BUILD_OUTPUT']) / 'sdk/aarch64'
    if not args.payload_only:
        config = args.config.read_text()
        (out / 'kernel.config').write_text(trace_config(config) if args.trace else config)
        run(['make', 'image', 'app', 'ARCH=aarch64', f'CONFIG_FILE={out}/kernel.config',
             'CARGO_FEATURES=', 'NATIVE_IMAGE_PROFILE=system'])
    env.update(HYPER_CLANG=clang, HYPER_LD=linker)
    flags = ['-O2', '-march=armv8-a', '-ffreestanding', '-fno-builtin', '-fno-stack-protector',
             '-fPIC', '-Wall', '-Wextra', '-Werror', '-I', PLATFORM, '-I', SOURCE]
    objects = []
    for source in ('records', 'workload', 'durability'):
        obj = out / f'{source}.o'
        run([clang, '--target=aarch64-none-elf', *flags, '-c', SOURCE / f'{source}.c', '-o', obj])
        objects.append(obj)
    run([clang, '--target=aarch64-none-elf', *flags, '-DBENCH_CUSTOM_ENTRY', '-c',
         PLATFORM / 'linux.c', '-o', out / 'linux.o'])
    run([clang, '--target=aarch64-none-elf', *flags, '-c', SOURCE / 'entry-linux.c', '-o', out / 'entry-linux.o'])
    # Pi 5's default Linux kernel uses 16 KiB pages. A 64 KiB segment layout
    # also works with the other AArch64 Linux page sizes without rebuilding.
    run([linker, '-static', '-e', '_start', '-z', 'max-page-size=65536', *objects,
         out / 'linux.o', out / 'entry-linux.o', '-o', out / 'storage-qual-linux'])
    run([sdk / 'bin/hyper-clang', *flags, '-DBENCH_CUSTOM_ENTRY', PLATFORM / 'hyper.c',
         SOURCE / 'entry-hyper.c', *objects, '-lhyper-std', '-o', out / 'storage-qual-hyper'])
    artifacts = [*objects, out / 'storage-qual-linux', out / 'storage-qual-hyper']
    if not args.payload_only:
        board = json.loads((ROOT / 'boards/rpi5.json').read_text())
        next(part for part in board['disk']['partitions']
             if part.get('content') == 'data')['size-mib'] = 2048
        if args.trace:
            board = trace_board(board, args.firmware_mib)
        (out / 'config.json').write_text(json.dumps(board, indent=2) + '\n')
        app_override = []
        if args.retirement_probe:
            probe_target = out / 'probe-cargo'
            probe_apps = out / 'probe-apps'
            # Stage the entire application/DSO set together; Rust generics can
            # move between binaries and shared libraries in a feature build.
            run(['make', '-o', 'app-fetch', 'app', 'ARCH=aarch64',
                 f'CLANG={clang}', f'HYPER_LD={linker}', f'SDK_OUTPUT={sdk}',
                 f'APP_CARGO_OUTPUT={probe_target}', f'APP_OUTPUT={probe_apps}',
                 'APP_FEATURES=hyper-io-runtime/physical-retirement-probe'])
            app_override = [f'APP_OUTPUT={probe_apps}']
        extra = f'0755 bin/storage-qual "{out}/storage-qual-hyper"'
        if args.trace:
            extra += f' 0644 system:bin/storage-trace.sh "{SOURCE}/capture.sh"'
            shutil.copyfile(SOURCE / 'capture.sh', out / 'storage-trace.sh')
            artifacts.append(out / 'storage-trace.sh')
        run(['make', '-o', 'image', '-o', 'app', 'board-initramfs', 'board-guest-images', *app_override,
             'ARCH=aarch64', 'BOARD=rpi5',
             f'BOARD_CONFIG={out}/config.json', f'BOARD_OUTPUT={out}',
             f'CONFIG_FILE={out}/kernel.config', 'NATIVE_IMAGE_PROFILE=system',
             'IO_VM_PACKAGE=', 'IO_VM_REFERENCE=',
             f'BOARD_EXTRA_ENTRIES={extra}'])
        if args.trace and (out / 'system-root/bin/storage-trace.sh').read_bytes() != (out / 'storage-trace.sh').read_bytes():
            raise ValueError('trace capture script differs from the packaged system payload')
        guest_archive = out / 'alpine-storage-rootfs.tar'
        guest_rootfs(ROOT / 'kernel/target/guest/aarch64/rootfs.tar',
                     out / 'storage-qual-linux', guest_archive)
        run(['python3', '-B', 'scripts/pack-guest-disk.py', '--board', out / 'deployment.json',
             '--rootfs', guest_archive, '--output', out / 'alpine.ext4'])
        shutil.copyfile(ROOT / 'kernel/target/aarch64-unknown-none-softfloat/kernel/hyper.img', out / 'hyper.img')
        run(['python3', '-B', 'scripts/rpi5-bringup.py', '--board', out / 'deployment.json',
             '--kernel', out / 'hyper.img', '--initramfs', out / 'bootstrap.cpio',
             '--system-root', out / 'system-root', '--mkfs-erofs', args.mkfs_erofs,
             '--output', out / 'disk.img', '--artifact', f'alpine={out}/alpine.itb',
             '--artifact', f'alpine-rootfs={out}/alpine.ext4'])
        artifacts += [out / name for name in ('disk.img', 'hyper.img', 'bootstrap.cpio',
                                             'config.json', 'deployment.json', 'kernel.config', 'io.itb',
                                             'alpine.itb', 'alpine.ext4', 'alpine-storage-rootfs.tar')]
    manifest = {
        'format': 'hyper.storage-qualification.v2', 'payload_bytes': 1024 ** 3,
        'measurement_format': 2, 'sync_scopes': {'1': 'file', '2': 'volume'},
        'physical_retirement_probe': args.retirement_probe,
        'trace_profile': args.trace,
        'firmware_mib': args.firmware_mib if args.trace else None,
        'system_payload': (None if args.payload_only else
                           json.loads((out / 'disk.img.system.json').read_text())),
        'guest_program': None if args.payload_only else '/usr/bin/storage-qual',
        'source': source_inventory(ROOT),
        'compiler': run([clang, '--version'], capture_output=True, text=True).stdout,
        'commands': commands, 'sha256': {},
        'sources': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in [*sorted(SOURCE.glob('*.c')), *sorted(SOURCE.glob('*.h')),
                                 *sorted(SOURCE.glob('*.py')), *sorted(SOURCE.glob('*.sh')), SOURCE / 'README.md',
                                 PLATFORM / 'platform.h', PLATFORM / 'hyper.c', PLATFORM / 'linux.c',
                                 ROOT / 'boards/rpi5.json',
                                 ROOT / 'bootstrap/stage1/io-runtime/Cargo.toml',
                                 ROOT / 'bootstrap/stage1/io-runtime/src/runtime/mod.rs',
                                 ROOT / 'bootstrap/stage1/io-runtime/tests/fixtures/physical_retirement.rs']},
    }
    for path in artifacts:
        with path.open('rb') as stream:
            manifest['sha256'][path.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    (out / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Prepared: {out}')


if __name__ == '__main__':
    main()
