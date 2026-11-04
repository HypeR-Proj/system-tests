#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0
"""Select FAT explicitly for the FAT provider's queue regression fixture."""
import json
from pathlib import Path
import sys
source = json.loads(Path(sys.argv[1]).read_text())
parts = [p for p in source['disk']['partitions'] if p.get('content') == 'data']
if len(parts) != 1:
    raise ValueError('FAT queue fixture requires exactly one data volume')
parts[0]['filesystem'] = 'fat'
Path(sys.argv[2]).write_text(json.dumps(source, indent=2) + '\n')
