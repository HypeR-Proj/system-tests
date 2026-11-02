#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
"""Build and verify F2FS on a new disposable AArch64 I/O VM disk."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def run(args):
    # Source entry points delegate first; shared helpers live in the OUT view.
    sys.path.insert(0, str(ROOT / 'scripts'))
    from buildsystem.toolchain import native_environment

    build_output = Path(os.environ['HYPER_BUILD_OUTPUT'])
    output = args.output.resolve()
    if output.exists():
        raise ValueError('choose a new output directory; existing media is never replaced')
    output.mkdir(parents=True)
    env = native_environment(ROOT)
    compiler = env['HYPER_CLANG']

    def command(*argv):
        subprocess.run([str(arg) for arg in argv], cwd=ROOT, env=env, check=True)

    command(sys.executable, ROOT / 'lib/fs-f2fs/tests/check.py')
    board = json.loads((ROOT / 'boards/qemu-minimal.json').read_text())
    board['disk']['partitions'].insert(0, {
        'label': 'hyper-data', 'size-mib': 128, 'filesystem': 'fat', 'content': 'data'})
    config = output / 'board.json'
    config.write_text(json.dumps(board, indent=2) + '\n')
    base = output / 'base'
    command('make', 'board-build', 'ARCH=aarch64', 'BOARD=qemu',
            f'BOARD_CONFIG={config}', f'BOARD_OUTPUT={base}', 'SYSTEM_CONFIG=',
            f'CLANG={compiler}', f'HYPER_LD={env["HYPER_LD"]}')
    command(sys.executable, ROOT / 'scripts/build-native-module.py', '--module',
            'fs-f2fs', '--features', 'hyper-fs-f2fs-system/io-statistics')
    worker = build_output / 'staging/modules/fs-f2fs/system/fs-f2fs-system'
    command(sys.executable, ROOT / 'scripts/build-native-module.py', '--module', 'system-tests')
    probe = build_output / 'staging/modules/system-tests/system/f2fs-smoke'
    fixture = output / 'fixture'
    command(sys.executable, ROOT / 'tests/fs-f2fs/prepare.py', '--base', base,
            '--output', fixture, '--worker', worker, '--probe', probe,
            '--library', build_output / 'build/lib-fs-f2fs/host/libhyper-f2fs.so')
    command(sys.executable, ROOT / 'tests/qemu/verify-f2fs.py', '--qemu', args.qemu,
            '--image', ROOT / 'kernel/target/aarch64-unknown-none-softfloat/kernel/hyper.img',
            '--initramfs', fixture / 'bootstrap.cpio', '--disk', fixture / 'disk.img',
            '--board', fixture / 'deployment.json', '--log', fixture / 'run.log')
    print(f'F2FS acceptance passed: {fixture}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='new fixture directory; defaults to OUT/fixtures/f2fs')
    parser.add_argument('--qemu', default='qemu-system-aarch64')
    args = parser.parse_args()
    build_output = Path(os.environ.get('OUT', ROOT / 'out')).resolve()
    args.output = args.output.resolve() if args.output else build_output / 'fixtures/f2fs'
    if os.environ.get('HYPER_BUILD_VIEW') != '1':
        subprocess.run([sys.executable, str(ROOT / 'project.py'), '--out', str(build_output),
                        'exec', '--', sys.executable, 'tests/fs-f2fs/run.py',
                        '--output', str(args.output), '--qemu', args.qemu], cwd=ROOT,
                       env={**os.environ, 'ARCH': 'aarch64'}, check=True)
    else:
        run(args)
