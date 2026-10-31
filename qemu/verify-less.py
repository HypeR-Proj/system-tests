#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Pager file/pipe navigation, asynchronous keyboard and terminal restoration."""
import re
import sys

from hyper_test.guest_console import append_console_output
from hyper_test.session import Session, native_command
from hyper_test.command import Shell


def main():
    qemu, image, initramfs, logfile = sys.argv[1:]
    with Session(native_command(qemu, image, initramfs), logfile,
                 output_filter=append_console_output) as session:
        session.await_text(rb'HypeR session: console ready\n', timeout=90)
        session.await_text(rb'hyper-sh\$ ')

        shell = Shell(session)

        def run(command, expected=None, timeout=60):
            output = shell.check(command, timeout=timeout)
            if expected is not None and not re.search(expected, output):
                raise AssertionError(f'{command}: {output!r}')

        def quit_tool():
            session.await_text(rb'hyper-sh\$ ', timeout=30)
            run('echo TERMINAL_RESTORED', rb'\nTERMINAL_RESTORED\n')

        lines = ''.join(f'line{n:03d}\\n' for n in range(1, 81))
        run(f"printf '{lines}' > /tmp/pager-file")
        for command in ('less /tmp/pager-file', 'cat /tmp/pager-file | less', 'less < /tmp/pager-file'):
            session.send(command.encode() + b'\r')
            first = session.await_text(rb'lines 1-(\d+) / 80 \(END\)', timeout=60)
            height = int(re.search(rb'lines 1-(\d+)', first)[1])
            session.send(b'\x1b[6~')
            session.await_text(fr'lines {height + 1}-'.encode())
            session.send(b'\x1b[5~')
            session.await_text(rb'lines 1-')
            session.send(b'G')
            session.await_text(rb'-80 / 80 \(END\)')
            session.send(b'g/line050\r')
            session.await_text(rb'lines 50-')
            session.send(b'q')
            quit_tool()

        run('cat /tmp/pager-file | less > /tmp/pager-copy; echo COPIED', rb'COPIED\n')
        run('test "$(cat /tmp/pager-file)" = "$(cat /tmp/pager-copy)" && echo COPY_OK', rb'COPY_OK\n')
        run("printf '" + "%1024s" * 8 + "\\n' WIDE > /tmp/pager-wide && echo WIDE_OK", rb'WIDE_OK\n')
        session.send(b'cat /tmp/pager-wide | less\r')
        session.await_text(rb'lines 1-1 / 1 \(END\)')
        session.send(b'q')
        quit_tool()
        # No EOF yet: pager keys must work while the producer is busy without writing.
        session.send(b"{ echo PIPE_OPEN; i=0; while [ $i -lt 10000 ]; do i=$((i+1)); done; } | less\r")
        session.await_text(rb'PIPE_OPEN')
        session.await_text(rb'\(reading\)')
        session.send(b'q')
        quit_tool()
    print('verified pager file/pipe navigation and terminal restoration')


if __name__ == '__main__':
    main()
