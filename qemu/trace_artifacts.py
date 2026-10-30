# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Bulk trace artifacts travel through the real filesystem, never the UART.

The scenario exports only after capture has stopped, unmounts the data volume,
then closes its owned QEMU before mtools reads the backing image. Geometry is
verified against both GPT copies rather than assuming a partition offset.
"""

import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from board_config import Board, SECTOR

spec = importlib.util.spec_from_file_location('trace_board_image', ROOT / 'scripts/pack-board-image.py')
image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)


def prepare_board(output, data_mib=64):
    """No Alpine disk/downloads: just the existing minimal I/O VM and two volumes."""
    if not 64 <= data_mib <= 8192:
        raise ValueError('fixture data volume must be 64..8192 MiB')
    document = json.loads((ROOT / 'boards/qemu-minimal.json').read_text())
    document['disk']['partitions'].append({
        'label': 'hyper-data', 'size-mib': data_mib, 'filesystem': 'fat', 'content': 'data',
    })
    Board.parse(document)
    output.write_text(json.dumps(document, indent=2) + '\n')


def boot_command(qemu, kernel, initramfs, disk, board):
    return [sys.executable, '-B', str(ROOT / 'scripts/run-io-vm.py'),
            '--qemu', str(qemu), '--image', str(kernel), '--initramfs', str(initramfs),
            '--disk', str(disk), '--board', str(board)]


class TraceArtifacts:
    def __init__(self, disk, board, directory):
        self.disk = Path(disk).resolve()
        self.board = Board.load(board)
        parts = [part for part in self.board.partitions if part.content == 'data' and part.filesystem == 'fat']
        if len(parts) != 1:
            raise ValueError('trace fixture needs exactly one FAT data volume')
        self.offset = parts[0].start * SECTOR
        self.directory = Path(directory)
        self.mcopy = image.image_tool('mcopy', 'mcopy', 'mtools')

    @staticmethod
    def save(shell, names):
        for name in names:
            TraceArtifacts.check_name(name)
            shell.check(f'cp {shlex.quote("/tmp/" + name)} {shlex.quote("/data/trace-artifact-" + name)}', timeout=120)
        # Completion fences data and FAT metadata through the worker and block
        # backend. Merely closing the Native file does not prove this fence.
        shell.check('umount /data', timeout=120)

    def extract(self, session, names):
        if session.process is None or session.process.poll() is None:
            raise ValueError('stop the owned QEMU before reading its disk image')
        with self.disk.open('rb') as stream:
            image.validate_disk(stream, self.board)
        self.directory.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name in names:
            self.check_name(name)
            target = self.directory / name
            subprocess.run([self.mcopy, '-o', '-i', f'{self.disk}@@{self.offset}',
                            f'::/trace-artifact-{name}', str(target)],
                           check=True, stdin=subprocess.DEVNULL, timeout=60)
            paths[name] = target
        return paths

    @staticmethod
    def check_name(name):
        if not name or name in ('.', '..') or Path(name).name != name or '\\' in name:
            raise ValueError('artifact names must be plain file names')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Prepare a minimal disk-backed trace fixture')
    parser.add_argument('--board-output', type=Path, required=True)
    parser.add_argument('--data-mib', type=int, default=64)
    args = parser.parse_args()
    prepare_board(args.board_output, args.data_mib)
