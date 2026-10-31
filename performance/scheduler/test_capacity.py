#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'qemu'))
from capacity import MIB, check_export_capacity, export_budget, parse_free_bytes
from trace_artifacts import Board, SECTOR, image, prepare_board


class CapacityTests(unittest.TestCase):
    def test_default_manual_bound_requires_more_than_old_fixture(self):
        # Four CPUs, three repeats: 156 baseline + 48 additional series.
        required = export_budget((13 + 4) * 4 * 3, 32768, 10000)
        self.assertGreater(required, 64 * MIB)
        self.assertLess(required, 500 * MIB)
        maximum = (1 << 64) - 1
        series = dict(distribution=dict(bins=[maximum] * 32768,
                                        raw=[dict(sequence=maximum, target_ns=maximum,
                                                  observed_ns=maximum)] * 10000),
                      metadata='x' * (16 * 1024 - 512))
        encoded = json.dumps(series, separators=(',', ':')).encode()
        self.assertLess(len(encoded), export_budget(1, 32768, 10000) - 8 * MIB)

    def test_free_space_parser_rejects_missing_or_ambiguous_evidence(self):
        self.assertEqual(parse_free_bytes('  66 056 192 bytes free\n'), 66056192)
        self.assertEqual(parse_free_bytes('  66,056,192 bytes free\n'), 66056192)
        for text in ('', '512 MiB total', '1 bytes free\n2 bytes free\n'):
            with self.assertRaises(ValueError):
                parse_free_bytes(text)

    def test_capacity_uses_free_clusters_and_validates_gpt_before_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, disk = root / 'board.json', root / 'disk.img'
            prepare_board(config, 512)
            board = Board.load(config)
            with disk.open('wb') as stream:
                stream.truncate(board.disk_sectors * SECTOR)
                for sector, data in image.gpt(board):
                    stream.seek(sector * SECTOR)
                    stream.write(data)
            partition = next(p for p in board.partitions if p.content == 'data')
            artifacts = Mock(disk=disk, board=board, offset=partition.start * SECTOR)
            required = export_budget(204, 32768, 10000)
            with patch('capacity.image.image_tool', return_value='/tools/mdir'), \
                 patch('capacity.subprocess.run') as run:
                # A large *partition* is insufficient when existing files use it.
                run.return_value.stdout = '  64 000 000 bytes free\n'
                with self.assertRaisesRegex(ValueError, 'before measurement'):
                    check_export_capacity(artifacts, required)
                run.return_value.stdout = f'  {500 * MIB} bytes free\n'
                self.assertEqual(check_export_capacity(artifacts, required)['free_bytes'], 500 * MIB)
                self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')
                run.side_effect = subprocess.CalledProcessError(1, 'mdir')
                with self.assertRaises(subprocess.CalledProcessError):
                    check_export_capacity(artifacts, required)
                with disk.open('r+b') as stream:
                    stream.seek(SECTOR)
                    stream.write(b'BROKEN!!')
                run.reset_mock()
                with self.assertRaisesRegex(ValueError, 'GPT'):
                    check_export_capacity(artifacts, required)
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
