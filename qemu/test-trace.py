#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Trace artifact fencing, GPT selection and byte-preserving extraction."""

from pathlib import Path
import tempfile
import json
import sys
import unittest
from unittest.mock import Mock, patch

from trace_artifacts import Board, SECTOR, TraceArtifacts, image, prepare_board


class TraceArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='hyper-trace-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'board.json'
        prepare_board(self.config)
        self.board = Board.load(self.config)
        self.disk = self.root / 'disk with spaces.img'
        with self.disk.open('wb') as stream:
            stream.truncate(self.board.disk_sectors * SECTOR)
            for sector, data in image.gpt(self.board):
                stream.seek(sector * SECTOR)
                stream.write(data)
        self.tools = patch('trace_artifacts.image.image_tool', return_value='/tools/mcopy')
        self.tools.start()
        self.addCleanup(self.tools.stop)
        self.store = TraceArtifacts(self.disk, self.config, self.root / 'output')
        self.session = Mock()
        self.session.process.poll.return_value = 0

    def test_raw_bytes_and_data_partition_offset(self):
        original = b'{"traceEvents":[]}\n\n'

        def copy(arguments, **kwargs):
            data = next(part for part in self.board.partitions if part.content == 'data')
            self.assertEqual(arguments[:5], ['/tools/mcopy', '-o', '-i',
                                            f'{self.disk.resolve()}@@{data.start * SECTOR}',
                                            '::/trace-artifact-example.json'])
            Path(arguments[-1]).write_bytes(original)

        with patch('trace_artifacts.subprocess.run', side_effect=copy):
            files = self.store.extract(self.session, ('example.json',))
        self.assertEqual(files['example.json'].read_bytes(), original)
        self.assertEqual(self.board.partitions[0].content, 'system')

    def test_live_or_unstarted_qemu_cannot_be_read(self):
        with patch('trace_artifacts.subprocess.run') as copy:
            for process in (None, Mock(poll=Mock(return_value=None))):
                self.session.process = process
                with self.assertRaisesRegex(ValueError, 'stop the owned QEMU'):
                    self.store.extract(self.session, ('example.json',))
            copy.assert_not_called()

    def test_incompatible_gpt_is_rejected_before_mcopy(self):
        with self.disk.open('r+b') as stream:
            stream.seek(SECTOR)
            stream.write(b'BROKEN!!')
        with patch('trace_artifacts.subprocess.run') as copy:
            with self.assertRaisesRegex(ValueError, 'GPT'):
                self.store.extract(self.session, ('example.json',))
            copy.assert_not_called()

    def test_copy_failure_does_not_return_a_stale_artifact(self):
        import subprocess
        self.store.directory.mkdir()
        (self.store.directory / 'example.json').write_text('old run')
        with patch('trace_artifacts.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'mcopy')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.store.extract(self.session, ('example.json',))

    def test_guest_fences_after_all_exports(self):
        shell = Mock()
        TraceArtifacts.save(shell, ('trace.json', 'ipc.json'))
        commands = [call.args[0] for call in shell.check.call_args_list]
        self.assertEqual(commands, ['cp /tmp/trace.json /data/trace-artifact-trace.json',
                                    'cp /tmp/ipc.json /data/trace-artifact-ipc.json',
                                    'umount /data'])
        for name in ('../escape', '/absolute', '..', 'sub/file', 'sub\\file'):
            with self.assertRaises(ValueError):
                self.store.check_name(name)


class PerfettoOwnershipTests(unittest.TestCase):
    @staticmethod
    def artifact(pid=100, owner='vm-io'):
        # An independent minimal protobuf fixture exercises the identity schema,
        # including Perfetto's implicit process-leader TID namespace.
        def integer(value):
            result = bytearray()
            while value > 127:
                result.append((value & 127) | 128)
                value >>= 7
            return bytes(result) + bytes([value])

        def field(number, value):
            if isinstance(value, int):
                return integer(number << 3) + integer(value)
            if isinstance(value, str):
                value = value.encode()
            return integer((number << 3) | 2) + integer(len(value)) + value

        metadata = json.dumps({'vm_threads': {'0x123': {'name': 'io', 'vcpu': 0}},
                               'exported_threads': {'0x123': 1}})
        descriptor = field(1, 1) + field(14, metadata)
        process = field(1, pid) + field(3, owner)
        thread = field(1, 1) + field(2, pid) + field(3, 'vcpu/0') + field(5, 0)
        tree = field(1, process) + field(2, thread)
        return (field(1, field(8, 0) + field(60, descriptor))
                + field(1, field(8, 0) + field(122, tree)))

    def test_process_leader_cannot_steal_scheduler_thread(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'profiling'))
        from perfetto_proto import inspect
        self.assertEqual(len(inspect(self.artifact())['threads']), 1)
        with self.assertRaisesRegex(ValueError, 'collide'):
            inspect(self.artifact(pid=1))

    def test_vcpu_owner_must_match_vm_observation(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'profiling'))
        from perfetto_proto import inspect
        with self.assertRaisesRegex(ValueError, 'incorrect vCPU owner'):
            inspect(self.artifact(owner='kernel'))


if __name__ == '__main__':
    unittest.main()
