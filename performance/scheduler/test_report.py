#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from report import validate, write_csv


def fixture():
    return dict(format='hyper.scheduler-bench.v1', results=[dict(
        start_ns=100, end_ns=200, fairness=None, memory_mode='prefaulted',
        job=dict(case='cyclic', repetition=0, cpu=1, peer_cpu=None, receiver=False),
        series=[dict(name='periodic', worker=0, scheduling='observed Fair',
                     elapsed_ns=100, work_units=2, overruns=1, skipped_periods=7,
                     distribution=dict(count=2, bins=[1, 0], overflow=1,
                                       bin_width_ns=10, arithmetic_saturated=False,
                                       min_ns=3, mean_ns=16, max_ns=30,
                                       p50_upper_ns=9, p90_upper_ns=None,
                                       p99_upper_ns=None, p999_upper_ns=None,
                                       raw_enabled=True, raw_dropped=1,
                                       raw=[dict(sequence=0, target_ns=100, observed_ns=103)]))])])


class ReportTests(unittest.TestCase):
    def test_handoff_requires_both_ends_and_equal_sample_counts(self):
        doc = fixture()
        sender = doc['results'][0]
        sender['job'].update(case='channel', peer_cpu=2, options={'cross_cpu': True})
        with self.assertRaises(ValueError):
            validate(doc)
        receiver = copy.deepcopy(sender)
        receiver['job'].update(cpu=2, peer_cpu=1, receiver=True)
        doc['results'].append(receiver)
        validate(doc)
        receiver['start_ns'] += 1
        with self.assertRaises(ValueError):
            validate(doc)
        receiver['start_ns'] -= 1
        doc['results'].append(copy.deepcopy(receiver))
        with self.assertRaises(ValueError):
            validate(doc)

    def test_overflow_is_not_capped_and_csv_preserves_null(self):
        doc = fixture()
        validate(doc)
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'run.json', Path(directory) / 'summary.csv'
            source.write_text(json.dumps(doc))
            write_csv([source], output)
            with output.open() as stream:
                row, = list(csv.DictReader(stream))
            self.assertEqual(row['p99_upper_ns'], '')
            self.assertEqual(row['max_ns'], '30')
            self.assertEqual(row['skipped_periods'], '7')
            with self.assertRaises(FileExistsError):
                write_csv([source], output)

    def test_corrupt_or_incomplete_statistics_rejected(self):
        for key, value in [('overflow', 0), ('p99_upper_ns', 19), ('raw_dropped', 0),
                           ('arithmetic_saturated', True), ('mean_ns', 100)]:
            doc = copy.deepcopy(fixture())
            doc['results'][0]['series'][0]['distribution'][key] = value
            with self.assertRaises(ValueError, msg=key):
                validate(doc)
        doc = fixture()
        doc['results'][0]['series'][0]['distribution']['raw'][0]['observed_ns'] = 99
        with self.assertRaises(ValueError):
            validate(doc)

    def test_no_samples_are_null_not_zero_latency(self):
        doc = fixture()
        s = doc['results'][0]['series'][0]
        s['overruns'] = 0
        s['distribution'].update(count=0, bins=[0, 0], overflow=0, raw=[], raw_dropped=0,
                                 min_ns=None, mean_ns=None, max_ns=None, p50_upper_ns=None)
        validate(doc)
        s['distribution']['max_ns'] = 0
        with self.assertRaises(ValueError):
            validate(doc)


if __name__ == '__main__':
    unittest.main()
