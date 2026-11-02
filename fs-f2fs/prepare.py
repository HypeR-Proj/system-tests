#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
"""Clone a prepared board and replace only the clone's data with fresh F2FS."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from board_config import Board, SECTOR, MIB
spec = importlib.util.spec_from_file_location('pack_board', ROOT / 'scripts/pack-board-image.py')
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


def prepare(args):
    base, output = args.base.resolve(), args.output.resolve()
    if output == base or output.is_relative_to(base):
        raise ValueError('fixture must be separate from the source board')
    board = Board.load(base / 'deployment.json')
    data = [p for p in board.partitions if p.content == 'data']
    if len(data) != 1 or data[0].sectors * SECTOR < 128 * MIB:
        raise ValueError('fixture requires one data partition of at least 128 MiB')
    with (base / 'disk.img').open('rb') as stream:
        pack.validate_disk(stream, board)
    for artifact in [args.worker, args.probe, args.library, base / 'bootstrap.cpio']:
        if not artifact.is_file():
            raise ValueError(f'missing artifact: {artifact}')
    output.mkdir(parents=True, exist_ok=False)
    disk = output / 'disk.img'
    if sys.platform == 'darwin':
        subprocess.run(['cp', '-c', str(base / 'disk.img'), str(disk)], check=True)
    else:
        shutil.copyfile(base / 'disk.img', disk)
    for name in ['deployment.json', 'bootstrap.cpio']:
        shutil.copyfile(base / name, output / name)
    system = output / 'system-root'
    shutil.copytree(base / 'system-root', system, symlinks=True)
    def install(source, target):
        if not target.parent.resolve().is_relative_to(system.resolve()):
            raise ValueError(f'fixture destination escapes system root: {target}')
        # The source tree can contain symlinks; replacing a payload must not
        # follow an inherited absolute link back into the original board.
        if target.is_symlink():
            target.unlink()
        shutil.copyfile(source, target)
    install(args.worker, system / 'svc/fs-f2fs')
    install(args.probe, system / 'bin/f2fs-smoke')
    for name in ['svc/fs-f2fs', 'bin/f2fs-smoke']:
        (system / name).chmod(0o755)
    notices = system / 'share/licenses/fs-f2fs'
    if not notices.resolve().is_relative_to(system.resolve()):
        raise ValueError('license destination escapes fixture system root')
    notices.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / 'lib/fs-f2fs/licenses/manifest.json').read_text())
    for name in manifest['files']:
        install(ROOT / name, notices / Path(name).name)
    media = output / 'f2fs.img'
    subprocess.run([sys.executable, str(ROOT / 'lib/fs-f2fs/host.py'),
                    '--library', str(args.library.resolve()), '--output', str(media),
                    '--size-mib', str(data[0].sectors * SECTOR // MIB)], check=True)
    with disk.open('r+b') as target, media.open('rb') as source:
        pack.validate_disk(target, board)
        target.seek(data[0].start * SECTOR)
        shutil.copyfileobj(source, target, 1024 * 1024)
    subprocess.run([sys.executable, str(ROOT / 'scripts/pack-board-image.py'),
                    '--board', str(output / 'deployment.json'), '--output', str(disk),
                    '--system-root', str(system), '--refresh-system-payload'], check=True)
    (output / 'fixture.json').write_text(json.dumps({
        'SPDX-FileCopyrightText': '2026 roolrz', 'SPDX-License-Identifier': 'Apache-2.0',
        'source-board': str(base), 'data-partition': data[0].name,
        'actual-data-filesystem': 'f2fs',
        'note': 'deployment.json retains source geometry; its FAT formatter was bypassed',
    }, indent=2) + '\n')
    print(f'Prepared disposable F2FS fixture: {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['base', 'output', 'worker', 'probe', 'library']:
        parser.add_argument('--' + name, type=Path, required=True)
    prepare(parser.parse_args())
