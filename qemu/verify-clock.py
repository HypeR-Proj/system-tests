#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""AArch64 std clock/thread regression, with and without a usable RTC."""
import subprocess
import sys
import tempfile
from pathlib import Path

from hyper_test.session import Session


def main():
    qemu, image, initramfs, log_prefix = sys.argv[1:]
    machine = 'virt,virtualization=on,gic-version=3'
    platform = ['-cpu', 'max', '-smp', '4', '-m', '1G',
                '-nodefaults', '-display', 'none']
    with tempfile.TemporaryDirectory(prefix='hyper-clock-') as temporary:
        dtb = str(Path(temporary) / 'host.dtb')
        subprocess.run([qemu, '-machine', machine + ',dumpdtb=' + dtb,
                        *platform], check=True)
        subprocess.run(['fdtput', '-t', 's', dtb, '/pl031@9010000',
                        'status', 'disabled'], check=True)
        base = [qemu, '-machine', machine, *platform,
                '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
                '-append', 'earlycon=pl011,mmio32,0x09000000',
                '-kernel', image, '-initrd', initramfs]
        for uncalibrated in (True, False):
            mode = 'uncalibrated' if uncalibrated else 'rtc'
            command = base + (['-dtb', dtb] if uncalibrated else [])
            with Session(command, log_prefix + '-' + mode + '.log') as session:
                session.await_text(
                    b'wall clock is uncalibrated' if uncalibrated
                    else b'UTC clock initialized', timeout=60)
                session.await_text(rb'hyper-sh\$ ', timeout=60)
                session.send(
                    b'/bin/std-test --uncalibrated-clock\n' if uncalibrated
                    else b'/bin/std-test --name clock\n')
                session.await_text(
                    b'HYPER_STD_UNCALIBRATED_CLOCK_OK' if uncalibrated
                    else b'HYPER_STD_OK hello clock', timeout=120)
                session.await_text(rb'hyper-sh\$ ', timeout=30)
            print(mode + ' passed', flush=True)


if __name__ == '__main__':
    main()
