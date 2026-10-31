#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Local host checks of qualification oracles; no hardware performance claims."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('storage_results', SOURCE / 'results.py')
results = importlib.util.module_from_spec(spec)
spec.loader.exec_module(results)

HOST = r'''
#define _POSIX_C_SOURCE 200809L
#include "qualification.h"
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
_Noreturn void bench_fail(const char *op, int64_t error) {
    (void)error; fprintf(stderr, "BENCH-FAIL: %s\n", op); exit(1);
}
void bench_output(const char *data, size_t size) {
    if (fwrite(data, 1, size, stdout) != size) exit(1);
}
uint64_t bench_clock(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) exit(1);
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
uint64_t bench_open(const char *path, int mode) {
    int fd = open(path, mode == BENCH_CREATE ? O_RDWR | O_CREAT | O_TRUNC :
                  mode == BENCH_READ_WRITE ? O_RDWR : O_RDONLY, 0600);
    if (fd < 0) bench_fail("open", fd);
    return fd;
}
void bench_close(uint64_t file) { if (close(file)) exit(1); }
void bench_read(uint64_t file, uint64_t offset, void *buf, size_t size) {
    if (pread(file, buf, size, offset) != (ssize_t)size) bench_fail("read", 0);
}
void bench_write(uint64_t file, uint64_t offset, const void *buf, size_t size) {
    if (pwrite(file, buf, size, offset) != (ssize_t)size) bench_fail("write", 0);
}
void bench_sync_volume(uint64_t file) { if (fsync(file)) exit(1); }
void bench_pause(uint64_t ns) { (void)ns; }
int main(int argc, char **argv) { return storage_main(argc, argv); }
'''


class Qualification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        (cls.root / 'host.c').write_text(HOST)
        cls.binary = cls.root / 'qual'
        subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O2',
                        '-fsanitize=undefined', '-fno-sanitize-recover=undefined',
                        '-DQUAL_BYTES=2097152',
                        '-I', str(SOURCE), '-I', str(SOURCE.parents[1] / 'performance'),
                        str(cls.root / 'host.c'), *(str(SOURCE / name) for name in
                                                  ('workload.c', 'records.c', 'durability.c')),
                        '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.file = self.root / 'storage-qual.bin'

    def run_case(self, mode, run=123, *args, success=True):
        result = subprocess.run([str(self.binary), mode, str(self.file), str(run), *map(str, args)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_full_readback_and_wrong_run(self):
        self.run_case('write')
        self.run_case('check')
        self.run_case('check', 124, success=False)
        self.assertEqual(self.file.stat().st_size, 2097152)

    def test_lost_ack_is_failure_even_when_file_opens(self):
        self.run_case('prepare')
        self.run_case('durable')
        with self.file.open('r+b') as stream:
            stream.write(b'corrupt!')
        result = self.run_case('recover', 123, 1, success=False)
        self.assertIn('LOST_ACK', result.stdout)

    def test_overwrite_and_sync4k_preserve_the_new_pattern(self):
        self.run_case('write')
        self.run_case('overwrite', 124)
        self.run_case('check', 123, success=False)
        result = self.run_case('sync4k', 124)
        self.assertEqual(result.stdout.count('STORAGE,SYNC4K_NS,124,'), 128)
        self.run_case('check', 124)
        self.assertEqual(self.file.stat().st_size, 2097152)

    def test_invalid_arguments_do_not_truncate_payload(self):
        self.run_case('write')
        before = self.file.read_bytes()
        for mode, run, extra in [('unknown', 123, []), ('write', 0, []),
                                 ('write', 123, [1]), ('recover', 123, [0]),
                                 ('write', 18446744073709551616, [])]:
            with self.subTest(mode=mode, run=run, extra=extra):
                self.run_case(mode, run, *extra, success=False)
                self.assertEqual(self.file.read_bytes(), before)

    def test_unacknowledged_torn_tail_is_reported_separately(self):
        self.run_case('prepare')
        self.run_case('durable')
        with self.file.open('r+b') as stream:
            stream.seek(1048576)
            stream.write(b'partial!')
        result = self.run_case('recover', 123, 1)
        self.assertIn('UNACK_TORN_CHUNKS,123,1', result.stdout)
        self.assertIn('ACK_PREFIX_VERIFIED,123,1', result.stdout)

    def test_durable_refuses_reusing_completed_payload(self):
        self.run_case('prepare')
        self.run_case('durable')
        before = self.file.read_bytes()
        self.run_case('durable', success=False)
        self.assertEqual(before, self.file.read_bytes())
        self.run_case('recover', 123, 0, success=False)

    def test_external_oracle_rejects_partial_missing_or_duplicate_ack(self):
        log = self.root / 'power-cut.log'
        prefix = ('STORAGE,BEGIN,7,durable\nSTORAGE,PAYLOAD_BYTES,7,1073741824\n'
                  'STORAGE,ISSUE,7,1\nSTORAGE,ACK,7,1\n')
        log.write_text(prefix + 'STORAGE,ISSUE,7,2\nSTORAGE,ACK,7,2')
        self.assertEqual(results.oracle(log, 7)['ack_mib'], 1)
        for suffix in ('STORAGE,ACK,7,1\n', 'STORAGE,ISSUE,7,3\n',
                       'STORAGE,COMPLETE,7,1073741824\n'):
            log.write_text(prefix + suffix)
            with self.assertRaises(ValueError):
                results.oracle(log, 7)

    def test_summary_requires_completed_verified_payload(self):
        log = self.root / 'measurement.log'
        prefix = ('STORAGE,BEGIN,7,write\nSTORAGE,PAYLOAD_BYTES,7,1073741824\n'
                  'STORAGE,WRITE_NS,7,2000000000\nSTORAGE,SYNC_NS,7,1000000000\n'
                  'STORAGE,TOTAL_NS,7,3000000000\n')
        log.write_text(prefix + 'STORAGE,END,7,0\n')
        with self.assertRaises(ValueError):
            results.measurements(log)
        log.write_text(prefix + 'STORAGE,VERIFIED,7,1073741824\nSTORAGE,END,7,0\n')
        self.assertAlmostEqual(results.measurements(log)[0]['mib_per_second'], 1024 / 3)


if __name__ == '__main__':
    unittest.main()
