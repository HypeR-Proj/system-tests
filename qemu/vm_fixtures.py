#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Derive fleet supervision fixtures from the selected architecture's VM policy."""
import copy
import json
from pathlib import Path
import sys


def prepare(source, directory):
    document = json.loads(Path(source).read_text())
    definitions = document['virtual-machines']
    if len(definitions) != 1 or definitions[0]['name'] != 'alpine' or 'disk' in definitions[0]:
        raise ValueError('fleet fixtures require one diskless alpine definition')
    alpine = copy.deepcopy(definitions[0])
    alpine['autostart'] = True
    survivor = copy.deepcopy(alpine)
    survivor['name'] = 'survivor'
    dormant = copy.deepcopy(alpine)
    dormant['autostart'] = False
    missing = copy.deepcopy(survivor)
    missing['image'] = '/vm/missing.itb'
    failed = copy.deepcopy(alpine)
    # CPU 255 is valid configuration syntax but unavailable on test QEMU hosts.
    # Admission must succeed; only this guest's runtime startup should fail.
    failed['configuration']['affinity'] = [{'vcpu': 0, 'cpus': [255]}]
    cases = {
        'victim-first': [alpine, survivor],
        'survivor-first': [survivor, alpine],
        'empty': [],
        'no-autostart': [dormant],
        'missing-image': [alpine, missing],
        'start-failure': [failed, survivor],
    }
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, machines in cases.items():
        fixture = dict(document, **{'virtual-machines': machines})
        (directory / f'{name}.json').write_text(json.dumps(fixture, indent=2) + '\n')
    (directory / 'malformed.json').write_text('{"format":"hyper.vm-config",\n')


if __name__ == '__main__':
    prepare(*sys.argv[1:])
