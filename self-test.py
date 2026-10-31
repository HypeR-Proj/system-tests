#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Check case dispatch, fixture builders and assertions without booting QEMU."""

from pathlib import Path
import subprocess
import sys

component = Path(__file__).resolve().parent
for pattern in ('ci/test-*.py', 'qemu/test-*.py', 'performance/scheduler/test_*.py'):
    for path in sorted(component.glob(pattern)):
        subprocess.run([sys.executable, '-B', str(path)], cwd=component.parent, check=True)
