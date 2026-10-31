#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Prove runtime-loss retirement at deterministic guest power boundaries."""

import importlib.util
from functools import partial
from pathlib import Path
import re
import sys
import time

from hyper_test.session import Session, native_command


spec = importlib.util.spec_from_file_location(
    'guest_smp', Path(__file__).with_name('verify-guest-smp.py'))
guest_smp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest_smp)

# Match shell replies across interleaved Linux printk records. Session retains
# the raw log, and the shared filter rejects panic/lockup before removing logs.
CONSOLE_OUTPUT = partial(guest_smp.append_console_output, filter_guest_logs=True)


def await_power_loss(session, mode, after):
    # Runtime stderr and the console client drain independently. Observe the
    # crash marker without consuming an already printed disconnect or prompt.
    session.observe(b'HYPER_POWER_CRASH state=' + mode.encode() + rb'\n',
                    timeout=90, after=after)
    if mode == 'powered-off':
        session.await_text(rb'\[vmm\] virtual machine disconnected', timeout=90)
        session.await_text(rb'hyper-sh\$ ', timeout=90)


def main():
    qemu, image, initramfs, logfile, mode = sys.argv[1:]
    if mode not in ('dormant', 'pending', 'powered-off'):
        raise ValueError('unknown power crash mode')
    command = native_command(qemu, image, initramfs)
    Path(logfile).parent.mkdir(parents=True, exist_ok=True)
    with Session(command, logfile, cleanup_timeout=5,
                 output_filter=CONSOLE_OUTPUT) as session:
        pending = session.pending

        def await_text(pattern, timeout=90):
            return session.await_text(pattern, timeout, match_only=True)

        def send(command):
            session.send(command + b'\n')

        def owners():
            send(b'free --bytes')
            line = await_text(rb'Owners:[^\n]*user=\d+ B guest=\d+ B[^\n]*\n')
            await_text(rb'hyper-sh\$ ')
            return tuple(map(int, re.search(rb'user=(\d+) B guest=(\d+) B', line).groups()))

        def wait_state(expected):
            for _ in range(90):
                send(b'vmm status alpine')
                status = await_text(rb'alpine\s+(?:starting|running|stopping|stopped|failed)')
                await_text(rb'hyper-sh\$ ')
                if status.endswith(expected):
                    return
                if status.endswith(b'failed') and expected != b'failed':
                    raise RuntimeError('runtime failed before the selected power boundary')
                time.sleep(0.1)
            raise TimeoutError(f'VM did not reach {expected!r}')

        try:
            # Config publication and shell readiness may print in either order.
            await_text(rb'(?s)(?=.*HypeR session: console ready)'
                       rb'(?=.*HypeR system-startup: VM fleet configured)')
            send(b'echo HYPER_POWER_CRASH_READY')
            await_text(rb'\nHYPER_POWER_CRASH_READY\nhyper-sh\$ ')
            baseline_user, baseline_guest = owners()
            if baseline_guest != 0:
                raise RuntimeError('fixture unexpectedly autostarted a guest')
            for cycle in range(3):
                cycle_start = session.mark()
                send(b'vmm start alpine')
                if mode == 'powered-off':
                    await_text(rb'hyper-sh\$ ')
                    wait_state(b'running')
                    send(b'vmm console alpine')
                    await_text(rb'~ # ', timeout=guest_smp.GUEST_BOOT_TIMEOUT_SECONDS)
                    send(b'printf "POWER_CRASH_ONLINE="; cat /sys/devices/system/cpu/online')
                    await_text(rb'\nPOWER_CRASH_ONLINE=0-3\n')
                    await_text(rb'~ # ')
                    send(b'echo 0 > /sys/devices/system/cpu/cpu1/online')
                await_power_loss(session, mode, cycle_start)
                wait_state(b'failed')
                for _ in range(90):
                    user, guest = owners()
                    if guest == 0 and user <= baseline_user + 2 * 1024 * 1024:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError(f'{mode} cycle {cycle} leaked VM/runtime memory')
            print(f'verified three {mode} runtime deaths, reclamation and restart')
        except Exception:
            sys.stderr.write(pending[-16384:].decode(errors='replace'))
            raise


if __name__ == '__main__':
    main()
