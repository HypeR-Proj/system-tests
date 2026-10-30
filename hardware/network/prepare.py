#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Add a pinned network exercise to an existing Pi 5 storage test image."""

import argparse
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
from board_config import Board

spec = importlib.util.spec_from_file_location('board_packer', ROOT / 'scripts/pack-board-image.py')
packer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packer)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def add_file(target, name, data, mode=0o644):
    item = tarfile.TarInfo(name)
    item.mode = mode
    item.size = len(data)
    target.addfile(item, io.BytesIO(data))


def rootfs(base, package, license_file, output):
    with tarfile.open(base) as source, tarfile.open(output, 'w') as target:
        members = {item.name.removeprefix('./'): item for item in source.getmembers()}
        release = source.extractfile(members['etc/alpine-release']).read().decode().strip()
        if not release.startswith('3.23.'):
            raise ValueError('iperf3 package requires the Alpine 3.23 guest rootfs')
        for dependency in ('lib/ld-musl-aarch64.so.1', 'usr/lib/libcrypto.so.3'):
            if dependency not in members:
                raise ValueError(f'missing guest dependency: {dependency}')
        for item in source:
            name = item.name.removeprefix('./').rstrip('/')
            if name in ('usr/bin/iperf3', 'usr/bin/network-exercise', 'opt/hyper-network') or \
                    name.startswith('opt/hyper-network/'):
                raise ValueError('base already contains network exercise tools')
            if item.isfile():
                with source.extractfile(item) as contents:
                    target.addfile(item, contents)
            else:
                target.addfile(item)
        directory = tarfile.TarInfo('opt/hyper-network')
        directory.type, directory.mode = tarfile.DIRTYPE, 0o755
        target.addfile(directory)
        # APK v2 concatenates signature, metadata and payload gzip/tar streams.
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(package.read_bytes())),
                          mode='r:', ignore_zeros=True) as apk:
            for entry in apk:
                if entry.name == '.PKGINFO':
                    add_file(target, 'opt/hyper-network/PACKAGE.txt', apk.extractfile(entry).read())
                    continue
                if entry.name not in ('usr', 'usr/bin', 'usr/lib', 'usr/bin/iperf3',
                                      'usr/lib/libiperf.so.0', 'usr/lib/libiperf.so.0.0.0'):
                    continue
                item = copy.copy(entry)
                item.name = 'opt/hyper-network/' + entry.name
                item.uid = item.gid = 0
                if item.isfile():
                    with apk.extractfile(entry) as contents:
                        target.addfile(item, contents)
                else:
                    target.addfile(item)
        for name in ('iperf3', 'network-exercise'):
            add_file(target, 'usr/bin/' + name, (SOURCE / f'{name}.sh').read_bytes(), 0o755)
        add_file(target, 'opt/hyper-network/iperf3.lock.json', (SOURCE / 'iperf3.lock.json').read_bytes())
        add_file(target, 'opt/hyper-network/LICENSE', license_file.read_bytes())


def main():
    if any(os.environ.get(key, '').lower() in ('1', 'true') for key in ('CI', 'GITHUB_ACTIONS')):
        raise SystemExit('Hardware qualification is local/manual only.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--license', dest='license_file', type=Path, required=True,
                        help='upstream iperf3 license and bundled notices from the lock')
    parser.add_argument('--output', type=Path, required=True)
    erofs_tool = ROOT / 'target/host-tools/erofs/bin/mkfs.erofs'
    parser.add_argument('--mkfs-erofs', default=os.environ.get(
        'EROFS_MKFS', str(erofs_tool) if erofs_tool.is_file() else 'mkfs.erofs'))
    args = parser.parse_args()
    base, out = args.base.resolve(), args.output.resolve()
    source_manifest = json.loads((base / 'build.json').read_text())
    if source_manifest.get('physical_retirement_probe') or source_manifest.get('guest_program') != '/usr/bin/storage-qual':
        raise ValueError('require an ordinary storage qualification image containing the guest payload')
    if not source_manifest.get('system_payload'):
        raise ValueError('base lacks the system payload record; rebuild the storage qualification image')
    board = Board.load(base / 'deployment.json')
    if source_manifest['system_payload'] != packer.system_state(base / 'system-root', board):
        raise ValueError('base system payload changed; rebuild the storage qualification image')
    names = ('hyper.img', 'bootstrap.cpio', 'config.json', 'deployment.json', 'kernel.config', 'io.itb',
             'alpine.itb', 'storage-qual-linux', 'storage-qual-hyper', 'alpine-storage-rootfs.tar')
    for name in names:
        if digest(base / name) != source_manifest['sha256'][name]:
            raise ValueError(f'base artifact changed: {name}')
    lock = json.loads((SOURCE / 'iperf3.lock.json').read_text())
    if digest(args.package) != lock['sha256']:
        raise ValueError('iperf3 package hash mismatch')
    if digest(args.license_file) != lock['license_sha256']:
        raise ValueError('iperf3 license hash mismatch')
    out.mkdir(parents=True, exist_ok=False)
    for name in names:
        if name != 'alpine-storage-rootfs.tar':
            shutil.copyfile(base / name, out / name)
    shutil.copytree(base / 'system-root', out / 'system-root', symlinks=True)
    if source_manifest['system_payload'] != packer.system_state(out / 'system-root', board):
        raise ValueError('base system payload changed while copying; retry from an unchanged build')
    shutil.copyfile(base / 'build.json', out / 'base-build.json')
    shutil.copyfile(args.package, out / 'iperf3.apk')
    shutil.copyfile(args.license_file, out / 'iperf3-LICENSE')
    rootfs(base / 'alpine-storage-rootfs.tar', args.package, args.license_file,
           out / 'alpine-io-rootfs.tar')
    commands = [
        ['python3', '-B', 'scripts/pack-guest-disk.py', '--board', str(out / 'deployment.json'),
         '--rootfs', str(out / 'alpine-io-rootfs.tar'), '--output', str(out / 'alpine.ext4')],
        ['python3', '-B', 'scripts/rpi5-bringup.py', '--board', str(out / 'deployment.json'),
         '--kernel', str(out / 'hyper.img'), '--initramfs', str(out / 'bootstrap.cpio'),
         '--system-root', str(out / 'system-root'), '--mkfs-erofs', args.mkfs_erofs,
         '--output', str(out / 'disk.img'), '--artifact', f'alpine={out}/alpine.itb',
         '--artifact', f'alpine-rootfs={out}/alpine.ext4'],
    ]
    for command in commands:
        subprocess.run(command, cwd=ROOT, check=True)
    manifest = {
        'format': 'hyper.io-qualification.v1', 'base_build': 'base-build.json',
        'storage_payload_bytes': 1024 ** 3, 'network_package': lock,
        'physical_retirement_probe': False, 'commands': commands,
        'system_payload': json.loads((out / 'disk.img.system.json').read_text()),
        'sha256': {path.name: digest(path) for path in sorted(out.iterdir()) if path.is_file()},
        'sources': {str(path.relative_to(ROOT)): digest(path)
                    for path in sorted(SOURCE.iterdir()) if path.is_file()},
    }
    (out / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Prepared: {out / "disk.img"}')


if __name__ == '__main__':
    main()
