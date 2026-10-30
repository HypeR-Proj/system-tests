#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Editor interaction, saves, failed-save recovery and terminal restoration."""
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

        def edit(path):
            session.send(f'editor {path}\r'.encode())
            session.await_text(rb'\^S Save.*\x1b\[\?25h', timeout=60)

        def quit_tool():
            session.await_text(rb'hyper-sh\$ ', timeout=30)
            run('echo TERMINAL_RESTORED', rb'\nTERMINAL_RESTORED\n')

        edit('/tmp/editor-file')
        session.send('hello\r界 world'.encode() + b'\x13')
        session.await_text(rb'Saved 15 bytes', timeout=30)
        session.send(b'\x11')
        quit_tool()
        run('cat /tmp/editor-file; echo END', 'hello\n界 worldEND\n'.encode())
        # Redirected stdin must not replace the editor's keyboard connection.
        edit('/tmp/editor-file < /tmp/editor-file')
        session.send(b'X\x11')
        session.await_text(rb'Unsaved changes')
        session.send(b'\x11')
        quit_tool()
        run('cat /tmp/editor-file; echo END', 'hello\n界 worldEND\n'.encode())
        edit('/tmp/editor-file')
        session.send(b'\x1b[F\x7f!\x13')
        session.await_text(rb'Saved 15 bytes')
        session.send(b'\x11')
        quit_tool()
        run('cat /tmp/editor-file; echo END', 'hell!\n界 worldEND\n'.encode())

        # A failed Save As must retain the original document path.
        edit('/tmp/editor-file')
        session.send(b'\x0f')
        session.await_text(rb'Save as:')
        session.send(b'\x7f' * len('/tmp/editor-file') + b'/missing-parent/new-name\r')
        session.await_text(rb'Save failed:')
        session.send(b'\x13')
        session.await_text(rb'Saved 15 bytes')
        session.send(b'\x11')
        quit_tool()

        # A save error must keep the editor alive and its dirty text intact.
        edit('/missing-parent/editor-file')
        session.send(b'keep-me\x13')
        session.await_text(rb'Save failed:')
        session.send(b'\x0f')
        session.await_text(rb'Save as:')
        # Backspace the prefilled path, then recover into /tmp.
        session.send(b'\x7f' * len('/missing-parent/editor-file') + b'/tmp/editor-recovered\r')
        session.await_text(rb'Saved 7 bytes')
        session.send(b'\x11')
        quit_tool()
        run('cat /tmp/editor-recovered; echo END', rb'keep-meEND\n')

        # Bracketed paste cannot invoke editor save/quit controls.
        edit('/tmp/editor-paste')
        session.send(b'\x1b[200~paste\r\ntext\x11\x13\x1b[201~')
        session.await_text(rb'paste')
        session.send(b'\x13')
        session.await_text(rb'Saved 10 bytes')
        session.send(b'\x11')
        quit_tool()
        run('cat /tmp/editor-paste; echo END', rb'paste\ntextEND\n')
    print('verified editor saves, failure recovery and terminal restoration')


if __name__ == '__main__':
    main()
