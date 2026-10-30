#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Build an isolated AArch64 Native/Linux comparison fixture from cached inputs."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).resolve().parent


def main():
    if any(os.environ.get(name, '').lower() in ('1', 'true') for name in ('CI', 'GITHUB_ACTIONS')):
        raise SystemExit('Performance measurements are local/manual only; do not run them in CI.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--io-package', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ, CARGO_NET_OFFLINE='true')
    commands = []

    def run(command, **kwargs):
        command = [str(item) for item in command]
        commands.append(command)
        print('+', ' '.join(command), flush=True)
        subprocess.run(command, cwd=ROOT, env=environment, check=True, **kwargs)

    clang = '/opt/homebrew/opt/llvm/bin/clang' if Path('/opt/homebrew/opt/llvm/bin/clang').exists() else 'clang'
    linker = subprocess.check_output(['sh', 'scripts/find-llvm-tool.sh', 'ld.lld'], cwd=ROOT, text=True).strip()
    sdk = ROOT / 'target/sdk/aarch64'
    shutil.copyfile(args.config, out / 'kernel.config')
    run(['make', 'image', 'app', 'ARCH=aarch64', f'CONFIG_FILE={out}/kernel.config', 'CARGO_FEATURES='])
    shutil.copyfile(ROOT / 'kernel/target/aarch64-unknown-none-softfloat/kernel/hyper.img', out / 'hyper.img')
    flags = ['--target=aarch64-none-elf', '-O2', '-march=armv8-a', '-ffreestanding', '-fno-builtin',
             '-fno-stack-protector', '-fPIC', '-Wall', '-Wextra', '-Werror']
    run([clang, *flags, '-c', SOURCE / 'workload.c', '-o', out / 'workload.o'])
    run([clang, *flags, '-c', SOURCE / 'linux.c', '-o', out / 'linux.o'])
    run([linker, '-static', '-e', '_start', '-z', 'max-page-size=4096',
         out / 'workload.o', out / 'linux.o', '-o', out / 'bench-linux'])
    environment.update(HYPER_CLANG=clang, HYPER_LD=linker)
    run([sdk / 'bin/hyper-clang', '-O2', '-march=armv8-a', '-Wall', '-Wextra', '-Werror',
         SOURCE / 'hyper.c', out / 'workload.o', '-lhyper-std', '-o', out / 'bench-hyper'])
    board = json.loads((ROOT / 'boards/qemu.json').read_text())
    board['virtual-machines'] = []
    # This historical Linux comparison uses the same MMIO controller on both
    # sides. Production PCI/SMMU acceptance belongs to the board suites.
    board['io-vm']['io-device'] = {'profile': 'virtio-mmio-scsi', 'compatible': 'virtio,mmio'}
    # Preserve the disk/memory baseline's identical network-free machines.
    board['io-vm'].pop('network-device', None)
    board['io-vm'].pop('networks', None)
    board['files'] = {'seed.bin': 'seed'}
    (out / 'board.json').write_text(json.dumps(board, indent=2) + '\n')
    seed = bytes((i * 17 + 31) % 251 for i in range(251))
    (out / 'seed.bin').write_bytes((seed * ((16 * 1024 * 1024 + 250) // 251))[:16 * 1024 * 1024])
    run(['make', '-o', 'app', 'board-initramfs', 'ARCH=aarch64',
         f'CONFIG_FILE={out}/kernel.config', f'BOARD_CONFIG={out}/board.json',
         f'BOARD_OUTPUT={out}', f'IO_VM_PACKAGE={args.io_package.resolve()}', 'NATIVE_IMAGE_PROFILE=system',
         f'BOARD_EXTRA_ENTRIES=0755 bin/perf-bench "{out}/bench-hyper"'])
    run(['python3', '-B', 'scripts/pack-board-image.py', '--board', out / 'board.json',
         '--output', out / 'base.img', '--artifact', f'seed={out}/seed.bin'])
    with (out / 'linux-overlay.cpio').open('wb') as overlay:
        run([ROOT / 'target/host-tools/newc-pack', '0755', 'perf-init', SOURCE / 'linux-init.sh',
             '0755', 'bin/perf-bench', out / 'bench-linux'], stdout=overlay)
    guest = ROOT / 'kernel/target/guest/aarch64'
    shutil.copyfile(guest / 'Image', out / 'linux.img')
    (out / 'linux.cpio.gz').write_bytes((guest / 'initramfs.cpio.gz').read_bytes()
                                      + gzip.compress((out / 'linux-overlay.cpio').read_bytes(), mtime=0))
    artifacts = ['workload.o', 'bench-hyper', 'bench-linux', 'hyper.img', 'linux.img',
                 'bootstrap.cpio', 'linux.cpio.gz', 'base.img', 'kernel.config', 'board.json']
    hashes = {}
    for name in artifacts:
        with (out / name).open('rb') as stream:
            hashes[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = {'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'compiler': subprocess.check_output([clang, '--version'], text=True),
                'commands': commands, 'sha256': hashes,
                'workload_sources': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in SOURCE.iterdir() if p.is_file()}}
    (out / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Prepared: {out}', flush=True)


if __name__ == '__main__':
    main()
