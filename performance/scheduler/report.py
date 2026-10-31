# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Validate Native benchmark evidence and export one CSV row per measured series."""
import argparse
import csv
import json
from pathlib import Path


def validate(document):
    if document['format'] != 'hyper.scheduler-bench.v1' or not document['results']:
        raise ValueError('missing or unsupported scheduler report')
    pairs = {}
    for result in document['results']:
        if result['end_ns'] <= result['start_ns'] or not result['series']:
            raise ValueError('invalid measurement window or empty worker')
        for series in result['series']:
            d = series['distribution']
            count = d['count']
            if any(n < 0 for n in d['bins']) or sum(d['bins']) + d['overflow'] != count:
                raise ValueError('histogram does not account for every sample')
            if d['arithmetic_saturated'] or d['bin_width_ns'] <= 0:
                raise ValueError('saturated statistics or invalid histogram width')
            if count:
                if not 0 <= d['min_ns'] <= d['mean_ns'] <= d['max_ns']:
                    raise ValueError('invalid latency bounds')
            elif any(d[k] is not None for k in ('min_ns', 'mean_ns', 'max_ns')):
                raise ValueError('empty latency distribution must have null statistics')
            for key, fraction in (('p50_upper_ns', 500), ('p90_upper_ns', 900),
                                  ('p99_upper_ns', 990), ('p999_upper_ns', 999)):
                rank = (count * fraction + 999) // 1000
                cumulative, upper = 0, None
                if count:
                    for index, n in enumerate(d['bins']):
                        cumulative += n
                        if cumulative >= rank:
                            upper = (index + 1) * d['bin_width_ns'] - 1
                            break
                if d[key] != upper:
                    raise ValueError('percentile is inconsistent with the histogram')
            raw = d['raw']
            if d['raw_enabled']:
                if len(raw) + d['raw_dropped'] != count:
                    raise ValueError('unaccounted raw sample loss')
            elif raw or d['raw_dropped']:
                raise ValueError('disabled raw recording contains samples')
            for index, sample in enumerate(raw):
                delta = sample['observed_ns'] - sample['target_ns']
                if sample['sequence'] != index or not d['min_ns'] <= delta <= d['max_ns']:
                    raise ValueError('invalid raw sample sequence or clock ordering')
            if series['overruns'] > count or series['elapsed_ns'] < 0:
                raise ValueError('invalid overrun or duration accounting')
            if not series['scheduling'] or series['scheduling'].startswith('unavailable:'):
                raise ValueError('actual scheduling parameters were not observed')
        if result['fairness'] is not None and not 0 < result['fairness'] <= 1.000000001:
            raise ValueError('invalid Jain progress index')
        job = result['job']
        if job['case'] == 'atomic' and len({s['distribution']['count'] for s in result['series']}) != 1:
            raise ValueError('atomic handoff send/receive counts differ')
        if job['case'] == 'channel':
            a, b = job['cpu'], job['peer_cpu']
            if job['receiver']:
                a, b = b, a
            key = (job['repetition'], job['options']['cross_cpu'], a, b)
            peers = pairs.setdefault(key, {})
            if job['receiver'] in peers:
                raise ValueError('duplicate channel endpoint report')
            peers[job['receiver']] = result
    for peers in pairs.values():
        if set(peers) != {False, True}:
            raise ValueError('missing channel peer report')
        sender, receiver = peers[False], peers[True]
        if (sender['start_ns'], sender['end_ns']) != (receiver['start_ns'], receiver['end_ns']):
            raise ValueError('channel peers measured different windows')
        if sender['series'][0]['distribution']['count'] != receiver['series'][0]['distribution']['count']:
            raise ValueError('channel send/receive counts differ')
    return document


def rows(document, source):
    for result in validate(document)['results']:
        job = result['job']
        for series in result['series']:
            d = series['distribution']
            yield dict(source=source, case=job['case'], repetition=job['repetition'],
                       cpu=job['cpu'], peer_cpu=job['peer_cpu'], receiver=job['receiver'],
                       series=series['name'], worker=series['worker'],
                       scheduling=series['scheduling'], memory_mode=result['memory_mode'],
                       samples=d['count'], min_ns=d['min_ns'], mean_ns=d['mean_ns'],
                       p99_upper_ns=d['p99_upper_ns'], max_ns=d['max_ns'],
                       histogram_overflow=d['overflow'], raw_dropped=d['raw_dropped'],
                       skipped_periods=series['skipped_periods'], overruns=series['overruns'],
                       elapsed_ns=series['elapsed_ns'], work_units=series['work_units'],
                       jain_progress=result['fairness'])


def write_csv(files, output):
    records = [row for file in files for row in rows(json.loads(file.read_bytes()), file.name)]
    if not records:
        raise ValueError('no measured series')
    with output.open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', type=Path, nargs='+')
    parser.add_argument('--csv', type=Path, required=True)
    args = parser.parse_args()
    write_csv(args.reports, args.csv)
