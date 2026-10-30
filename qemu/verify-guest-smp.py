#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Exercise Linux SMP, host-CPU migration, hotplug, reboot, and poweroff."""

import os
from functools import partial
from pathlib import Path
import re
import sys
import time

from hyper_test.command import Shell
from hyper_test.session import Session, native_command

from hyper_test.guest_console import append_console_output


# Shared CI TCG hosts can exceed 180 seconds while a fresh Alpine SMP guest
# is still progressing (observed initrd release at guest t=172.6s on reboot).
# Keep a fixed total boot deadline; ordinary commands retain 90 seconds.
GUEST_BOOT_TIMEOUT_SECONDS = 300


def capture_failure_state(session, console_attached):
    """Collect host observations after failure without restarting the guest.

    The caller must preserve its original exception. Disable stream failure
    checks only during this bounded, best-effort collection: an already
    observed guest watchdog must not prevent detachment and host inspection.
    The raw serial archive and structured command artifacts retain the evidence.
    """
    failures, output_filter = session.failures, session.output_filter
    session.failures, session.output_filter = (), None
    try:
        # The failure marker can arrive before the rest of its stack trace.
        # Archive a bounded raw tail before detaching the guest console.
        session.pump(0.5)
        session.pending.clear()
        if console_attached:
            session.send(b'\x1dd')
        else:
            # Discard a partial host command before requesting a fresh prompt.
            session.send(b'\x03\n')
        session.await_text(rb'hyper-sh\$ ', timeout=10)
        shell = Shell(session)
        for command in ('vmm status alpine', 'ps --threads',
                        'top -b -n 2', 'journalctl -n 60'):
            result = shell.run(command, timeout=10)
            sys.stderr.write(f'\nSMP failure observation: {command} (exit={result.status})\n')
            sys.stderr.write(result.output.decode(errors='replace'))
    except Exception as error:
        sys.stderr.write(f'\nSMP failure observations unavailable: {error}\n')
    finally:
        session.failures, session.output_filter = failures, output_filter


def verify_vcpu_startup(log, guest_cpus, host_cpus):
    # First-run records are separated in time. Automatic migration can reuse
    # a host CPU between records, so their union is not a placement snapshot.
    # Keep execution evidence for every vCPU within one VM incarnation.
    records = re.findall(
        rb'HypeR: vCPU (\d+) running as scheduler thread \d+ '
        rb'on guarded stack [^;]+; host CPU (\d+); '
        rb'VM VmId \{ slot: (\d+), generation: (\d+) \}', log)
    instances = {}
    for cpu, host, slot, generation in records:
        if int(host) >= host_cpus:
            raise RuntimeError(f'invalid host CPU placement: vCPU {int(cpu)} on {int(host)}')
        instances.setdefault((slot, generation), {})[int(cpu)] = int(host)
    if not any(set(placements) == set(range(guest_cpus)) for placements in instances.values()):
        raise RuntimeError('missing complete per-vCPU host placement records')


def verify_host_affinity(status, targets):
    placements = sorted((int(cpu), host.decode()) for cpu, host in re.findall(
        rb'(?m)^ *vCPU (\d+): pCPU ([^\n]+)\n', status))
    expected = sorted((cpu, str(host)) for cpu, host in targets.items())
    if placements != expected:
        raise RuntimeError(f'vCPU affinity mismatch: {placements}; expected {expected}')


def main():
    qemu, image, initramfs, logfile = sys.argv[1:]
    guest_cpus = int(os.environ.get('GUEST_CPUS', '4'))
    host_cpus = int(os.environ.get('QEMU_CPUS', '4'))
    if not 2 <= guest_cpus <= 8:
        raise ValueError('SMP acceptance requires 2..8 guest CPUs')
    command = native_command(qemu, image, initramfs)
    Path(logfile).parent.mkdir(parents=True, exist_ok=True)
    with Session(command, logfile, cleanup_timeout=5,
                 output_filter=partial(append_console_output, filter_guest_logs=True)) as session:
        console_attached = False

        def await_text(pattern, timeout=90):
            return session.await_text(pattern, timeout, match_only=True)

        def send(command):
            session.send(command + b'\n')

        def attach():
            nonlocal console_attached
            # A reboot creates a fresh runtime asynchronously. Status polling
            # here is a test deadline, not the runtime notification mechanism.
            for _ in range(90):
                send(b'vmm status alpine')
                status = await_text(rb'alpine\s+(?:starting|running|stopping|stopped|failed)')
                await_text(rb'hyper-sh\$ ')
                if status.endswith(b'failed'):
                    raise RuntimeError('VM failed during guest reset')
                if status.endswith(b'running'):
                    send(b'vmm console alpine')
                    result = await_text(rb'Connected to alpine\.|hyper-sh\$ ')
                    if result.startswith(b'Connected'):
                        console_attached = True
                        # Reattachment need not replay an already consumed
                        # shell prompt; request a fresh empty command response.
                        send(b'')
                        await_text(rb'~ # ', timeout=GUEST_BOOT_TIMEOUT_SECONDS)
                        return
                    # The old runtime may exit between status and attach.
                    # A failed attach returns to the host shell; retry until
                    # the fresh instance publishes its console endpoint.
                time.sleep(0.1)
            raise TimeoutError('VM did not restart')

        def host_status():
            send(b'vmm status alpine')
            return session.await_text(rb'hyper-sh\$ ', 90)

        def migration_targets(cycle):
            # Spread over every available host CPU, and also co-locate the
            # whole guest to exercise virtual hardware ownership while sharing.
            return {cpu: 0 if cycle == 1 else (cpu + cycle + 1) % host_cpus
                    for cpu in range(guest_cpus)}

        def wait_host_affinity(targets):
            deadline = time.monotonic() + 30
            while True:
                status = host_status()
                try:
                    verify_host_affinity(status, targets)
                    return
                except RuntimeError as error:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'vCPU migration did not complete: {error}') from error
                time.sleep(0.1)

        def online(token, expected):
            send(f'printf "{token}="; /bin/busybox cat /sys/devices/system/cpu/online'.encode())
            await_text(rb'\n' + token.encode() + b'=' + re.escape(expected.encode()) + rb'\n')
            await_text(rb'~ # ')

        def migrate_guest_cpus(cycle):
            nonlocal console_attached
            if host_cpus < 2:
                return
            # Leave a guest timer pending across detachment and migration.
            # The kernel self-test separately migrates a guest that never WFI's.
            marker = f'/tmp/hyper-migration-{cycle}'
            send(f'(sleep 2; echo TIMER_OK > {marker}) &'.encode())
            await_text(rb'~ # ')
            session.send(b'\x1dd')
            await_text(rb'hyper-sh\$ ')
            console_attached = False
            targets = migration_targets(cycle)
            for cpu, target in targets.items():
                send(f'vmm affinity alpine {cpu} {target}'.encode())
                await_text(fr'vCPU {cpu}: affinity accepted; inspect vmm status for placement\n'.encode())
                await_text(rb'hyper-sh\$ ')
            # Singleton affinities make all placements stable together. Verify
            # the complete spread/co-location instead of independent samples.
            wait_host_affinity(targets)
            attach()
            send(f'while [ ! -f {marker} ]; do sleep 1; done; cat {marker}'.encode())
            await_text(rb'\nTIMER_OK\n')
            await_text(rb'~ # ')

        def wait_state(expected):
            for _ in range(90):
                send(b'vmm status alpine')
                status = await_text(rb'alpine\s+(?:starting|running|stopping|stopped|failed)')
                await_text(rb'hyper-sh\$ ')
                if status.endswith(expected):
                    return
                if status.endswith(b'failed') and expected != b'failed':
                    raise RuntimeError('VM unexpectedly failed')
                time.sleep(0.1)
            raise TimeoutError(f'VM did not reach {expected!r}')

        def memory_owners():
            send(b'free --bytes')
            line = await_text(rb'Owners:[^\n]*user=\d+ B guest=\d+ B[^\n]*\n')
            await_text(rb'hyper-sh\$ ')
            return tuple(map(int, re.search(rb'user=(\d+) B guest=(\d+) B', line).groups()))

        try:
            await_text(rb'HypeR session: console ready')
            await_text(rb'hyper-sh\$ ')
            attach()
            for cycle in range(3):
                online(f'SMP_ONLINE_{cycle}', f'0-{guest_cpus - 1}')
                if cycle == 0:
                    session.log.flush()
                    verify_vcpu_startup(Path(logfile).read_bytes(), guest_cpus, host_cpus)
                migrate_guest_cpus(cycle)
                online(f'SMP_MIGRATED_{cycle}', f'0-{guest_cpus - 1}')
                # Offline all secondaries, then bring them back repeatedly.
                # This exercises fresh PSCI contexts and per-vCPU timers/IPIs.
                for cpu in range(1, guest_cpus):
                    send(f'echo 0 > /sys/devices/system/cpu/cpu{cpu}/online'.encode())
                    await_text(rb'~ # ')
                online(f'SMP_OFFLINE_{cycle}', '0')
                for cpu in range(1, guest_cpus):
                    send(f'echo 1 > /sys/devices/system/cpu/cpu{cpu}/online'.encode())
                    await_text(rb'~ # ')
                online(f'SMP_REONLINE_{cycle}', f'0-{guest_cpus - 1}')
                # Run multiple independent tasks so Linux can exercise scheduler
                # IPIs and timer preemption while the console remains responsive.
                # The minimal Alpine initramfs has no taskset applet.
                send(('for c in ' + ' '.join(map(str, range(guest_cpus)))
                      + '; do /bin/busybox sh -c '
                      + "'i=0; while [ $i -lt 10000 ]; do i=$((i+1)); done; echo CPU_WORK_OK' & "
                      + '; done; wait; echo SMP_WORK_DONE').replace('& ;', '&').encode())
                for _ in range(guest_cpus):
                    await_text(rb'(?:^|\n)CPU_WORK_OK\n')
                await_text(rb'(?:^|\n)SMP_WORK_DONE\n')
                await_text(rb'~ # ')
                if host_cpus > 1:
                    session.send(b'\x1dd')
                    await_text(rb'hyper-sh\$ ')
                    console_attached = False
                    # Hotplug and sustained progress must preserve every
                    # accepted affinity despite periodic automatic balancing.
                    verify_host_affinity(host_status(), migration_targets(cycle))
                    attach()
            for _ in range(2):
                send(b'reboot')
                await_text(rb'\[vmm\] virtual machine disconnected')
                console_attached = False
                await_text(rb'hyper-sh\$ ')
                attach()
                online('SMP_AFTER_REBOOT', f'0-{guest_cpus - 1}')
            send(b'poweroff')
            await_text(rb'\[vmm\] virtual machine disconnected')
            console_attached = False
            await_text(rb'hyper-sh\$ ')
            wait_state(b'stopped')
            _user, guest = memory_owners()
            if guest != 0:
                raise RuntimeError('guest poweroff leaked guest pages')
            session.log.flush()
            if b'masked virtual timer PPI without an active vCPU' in Path(logfile).read_bytes():
                raise RuntimeError('guest timer interrupt escaped vCPU ownership')
            print(f'verified {guest_cpus} guest CPUs, migration, hotplug, two reboots, '
                  'and poweroff reclamation')
        except Exception:
            # The match buffer filters printk records. Retain the raw boot
            # progress in CI errors so a slow guest is distinguishable from
            # a stalled console even when no prompt has arrived yet.
            session.log.flush()
            sys.stderr.write(Path(logfile).read_bytes()[-16384:].decode(errors='replace'))
            capture_failure_state(session, console_attached)
            raise


if __name__ == '__main__':
    main()
