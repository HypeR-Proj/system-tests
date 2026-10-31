#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Publish/check immutable Native test images from the current checkout."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import re


TARGETS = {
    'aarch64': 'aarch64-unknown-none-softfloat',
    'riscv64': 'riscv64imac-unknown-none-elf',
}
FILES = ('hyper.img', 'initramfs.cpio')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def configuration_identity(output, arch, environment):
    settings = {}
    selected = {
        'kernel': environment.get('CONFIG_FILE', str(output / 'config' / arch / 'modules/kernel.config')),
        'product': environment.get('SYSTEM_CONFIG', ''),
    }
    for scope, filename in selected.items():
        values = {}
        if filename:
            for line in Path(filename).read_text().splitlines():
                if match := re.fullmatch(r'(CONFIG_\w+)=(.*)', line.strip()):
                    values[match[1]] = match[2]
                elif match := re.fullmatch(r'# (CONFIG_\w+) is not set', line.strip()):
                    values[match[1]] = 'n'
        settings[scope] = values
    settings['options'] = {name: environment.get(name, '') for name in
                           ('CARGO_FEATURES', 'KERNEL_FEATURES', 'RUSTFLAGS', 'STACK_AUDIT',
                            'APP_FEATURES', 'APP_EXTRA_BINS', 'APP_BUILD_PROFILE')}
    return settings


def publish(root, directory, arch, revision):
    directory.mkdir(parents=True, exist_ok=True)
    sources = (
        root / 'kernel/target' / TARGETS[arch] / 'kernel/hyper.img',
        root / 'target/app' / arch / 'initramfs.cpio',
    )
    for name, source in zip(FILES, sources):
        shutil.copyfile(source, directory / name)
    manifest = {
        'version': 1, 'arch': arch, 'revision': revision,
        'files': {name: digest(directory / name) for name in FILES},
    }
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


def verify(directory, arch, revision):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if (manifest.get('version') != 1 or manifest.get('arch') != arch
            or manifest.get('revision') != revision):
        raise ValueError('Native fixture does not match this revision and architecture')
    if set(manifest.get('files', {})) != set(FILES):
        raise ValueError('Native fixture has an unexpected file set')
    for name in FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise ValueError(f'Native fixture is missing a regular nonempty {name}')
        if digest(path) != manifest['files'][name]:
            raise ValueError(f'Native fixture checksum mismatch: {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('pack', 'check'))
    parser.add_argument('--arch', required=True, choices=TARGETS)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    import project
    revision = os.environ.get('HYPER_SOURCE_IDENTITY')
    source = Path(os.environ.get('HYPER_WORKSPACE', root))
    output = Path(os.environ.get('HYPER_BUILD_OUTPUT', source / 'out'))
    if not revision:
        revision = project.inventory(source, output)['identity']
    configuration = configuration_identity(output, args.arch, os.environ)
    revision = hashlib.sha256(json.dumps([revision, configuration], sort_keys=True).encode()).hexdigest()
    if args.action == 'pack':
        publish(root, args.directory, args.arch, revision)
    verify(args.directory, args.arch, revision)


if __name__ == '__main__':
    main()
