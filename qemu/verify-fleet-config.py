#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Check fleet admission outcomes independently of guest runtime success."""
from pathlib import Path
import re
import sys
import time

from hyper_test.session import Session, native_command
from hyper_test.command import Shell


CONFIGURED = b'HypeR system-startup: VM fleet configured'
REJECTED = b'HypeR vm-manager: fleet configuration rejected:'
BOOT_FAILED = b'[HypeR bootstrap stage 1][init] bootstrap failed'
INIT_FAILURES = (BOOT_FAILED, b'HypeR system-startup: bootstrap failed',
                 b'[HypeR bootstrap stage 1][init] critical service ',
                 b'HypeR system-startup: critical service ')
DEGRADED = b'HypeR system-startup: VM fleet configuration unavailable; Native services remain available'
GUEST_RUNNING = b'HypeR: vCPU 0 running as scheduler thread'
CASES = ('empty', 'no-autostart', 'malformed', 'missing-image', 'start-failure')
KERNEL_FAILURES = (b'HypeR: fatal', b'kernel panic', b'HypeR crash monitor')


def validate_log(case, output):
    if b'[HypeR bootstrap stage 1][fallback-sh] recovery console ready' in output:
        raise AssertionError('fleet failure reopened bootstrap recovery')
    if any(marker in output for marker in KERNEL_FAILURES):
        raise AssertionError('kernel failure during fleet provisioning')
    if case in ('malformed', 'missing-image'):
        if REJECTED not in output or DEGRADED not in output or any(marker in output for marker in INIT_FAILURES):
            raise AssertionError('configuration rejection did not retain Native services')
        if CONFIGURED in output or GUEST_RUNNING in output:
            raise AssertionError('rejected fleet was acknowledged or partially started')
    else:
        if CONFIGURED not in output or REJECTED in output or any(marker in output for marker in INIT_FAILURES):
            raise AssertionError('admitted fleet did not complete init provisioning')
        if case in ('empty', 'no-autostart') and GUEST_RUNNING in output:
            raise AssertionError('idle fleet unexpectedly started a guest')


def main():
    qemu, image, initramfs, logfile, case = sys.argv[1:]
    if case not in CASES:
        raise ValueError('unknown fleet configuration case')
    command = native_command(qemu, image, initramfs)
    rejected = case in ('malformed', 'missing-image')
    failures = KERNEL_FAILURES + INIT_FAILURES
    if not rejected:
        failures += (REJECTED,)
    with Session(command, logfile, failures=failures) as session:
        shell = Shell(session)
        if rejected:
            session.await_text(rb'(?s)(?=.*' + re.escape(REJECTED)
                               + rb')(?=.*' + re.escape(DEGRADED) + rb')')
            # Provisioning errors must leave the console and manager responsive.
            session.send(b"echo FLEET-''RECOVERY-OK\n")
            session.await_text(rb'FLEET-RECOVERY-OK\n')
            session.await_text(rb'hyper-sh\$ ')
            listing = shell.check('vmm list')
            if re.search(rb'^\S+\s+(?:starting|running|stopping|stopped|failed)\s+',
                         listing, flags=re.M):
                raise AssertionError('rejected fleet published definitions')
            response = shell.check('vmm start alpine', failed=True)
            if b'VM fleet unavailable: initial configuration is unavailable' not in response:
                raise AssertionError(f'rejected fleet did not report unavailability: {response!r}')
        else:
            session.await_text(rb'(?s)(?=.*HypeR session: console ready)'
                               rb'(?=.*' + re.escape(CONFIGURED) + rb')')
            session.await_text(rb'hyper-sh\$ ')

            def run(command, timeout=60):
                return shell.check(command, timeout=timeout)

            if case == 'start-failure':
                # A valid image and policy were admitted. CPU affinity fails
                # only inside alpine's runtime; the next autostart must run.
                deadline = time.monotonic() + 60
                while (remaining := deadline - time.monotonic()) > 0:
                    failed = run(b'vmm status alpine', remaining)
                    survivor = run(b'vmm status survivor', max(0.01, deadline - time.monotonic()))
                    if (re.search(rb'\nalpine\s+failed\s', failed)
                            and re.search(rb'\nsurvivor\s+running\s', survivor)
                            and re.search(rb'allocated VM backing: \d+ bytes', survivor)):
                        break
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                else:
                    raise AssertionError('one autostart failure blocked the other VM')
            else:
                listing = run(b'vmm list')
                rows = re.findall(rb'^\S+\s+(?:starting|running|stopping|stopped|failed)\s+',
                                  listing, flags=re.M)
                if case == 'empty' and rows:
                    raise AssertionError(f'empty fleet published definitions: {listing!r}')
                if case == 'no-autostart' and not re.search(
                        rb'\nalpine\s+stopped\s+no\s', listing):
                    raise AssertionError(f'non-autostart definition was not idle: {listing!r}')
                memory = run(b'free --bytes')
                if not re.search(rb'Owners:[^\n]*guest=0 B', memory):
                    raise AssertionError(f'idle fleet owns guest pages: {memory!r}')
            output = run(b'echo HYPER_FLEET_CONFIG_OK')
            if b'\nHYPER_FLEET_CONFIG_OK\n' not in output:
                raise AssertionError('Native shell did not survive fleet provisioning')
    validate_log(case, Path(logfile).read_bytes())
    print(f'verified fleet configuration case: {case}')


if __name__ == '__main__':
    main()
