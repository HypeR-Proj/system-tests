#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Exercise Native terminal behavior through real standard streams and leases."""

import re
import sys

from hyper_test.guest_console import append_console_output
from hyper_test.session import Session, native_command


def main():
    qemu, image, initramfs, logfile = sys.argv[1:]
    with Session(native_command(qemu, image, initramfs), logfile,
                 output_filter=append_console_output) as session:
        session.await_text(rb'HypeR session: console ready\n', timeout=90)
        session.await_text(rb'hyper-sh\$ ', timeout=30)

        def command(mode, result):
            session.send(f'terminal-probe {mode}\r'.encode())
            session.await_text(result, timeout=30)
            session.await_text(rb'hyper-sh\$ ', timeout=30)

        command('metadata', rb'TERMINAL_METADATA_OK\n')
        command('timing', rb'TERMINAL_TIMING_OK\n')
        command('backpressure', rb'TERMINAL_BACKPRESSURE_OK\n')
        command('lease-tree', rb'TERMINAL_LEASE_TREE_OK\n')
        session.send(b'terminal-probe formatting\r')
        session.await_text(rb'TERMINAL_EDIT_READY\n', timeout=30)
        session.send(b'foo/bar! \x17\x16\n\x12X\r')
        session.await_text(re.escape(b'foo/bar! ' + b'\x08 \x08' * 5 +
                                     b'^\x08^J^R\nfoo/^JX\n'), timeout=30)
        session.await_text(rb'TERMINAL_ERASE_ECHO_READY\n', timeout=30)
        session.send(b'ab\x7f\x15z\r')
        session.await_text(re.escape(b'ab^?^U\nz\n'), timeout=30)
        session.await_text(rb'TERMINAL_HARDCOPY_READY\n', timeout=30)
        session.send(b'ab\x7fc\r')
        session.await_text(re.escape(b'ab\\b/c\n'), timeout=30)
        session.await_text(rb'TERMINAL_FORMATTING_OK\n', timeout=30)
        session.await_text(rb'hyper-sh\$ ', timeout=30)
        session.send(b'terminal-probe canonical\r')
        session.await_text(rb'TERMINAL_CANONICAL_READY\n', timeout=30)
        # EOF closes a record, not the terminal itself. Typeahead remains in
        # the server until exactly the amount requested by each read is sent.
        session.send(b'abc\x04\x04next\r')
        session.await_text(rb'TERMINAL_CANONICAL_OK\n', timeout=30)
        session.await_text(rb'hyper-sh\$ ', timeout=30)
        session.send(b'terminal-probe raw\r')
        session.await_text(rb'TERMINAL_RAW_READY\n', timeout=30)
        session.send(bytes(range(256)))
        session.await_text(rb'TERMINAL_RAW_OK\n', timeout=30)
        session.await_text(rb'hyper-sh\$ ', timeout=30)
        session.send(b'terminal-probe flow\r')
        session.await_text(rb'TERMINAL_FLOW_READY\n', timeout=30)
        session.send(b'\x13g')
        session.pump(0.5)
        # Input and the control path are usable before XON unblocks the writer.
        session.send(b'r')
        session.pump(0.5)
        if b'TERMINAL_FLOW_OK' in session.pending:
            raise AssertionError('STOP failed to pause terminal output')
        session.send(b'\x11')
        session.await_text(rb'TERMINAL_FLOW_OK\n', timeout=60)
        session.await_text(rb'hyper-sh\$ ', timeout=30)
        command('raw-exit', rb'TERMINAL_RAW_EXIT\n')
        session.send(b'terminal-probe control-drop\r')
        session.await_text(rb'TERMINAL_CONTROL_DROP_READY\n', timeout=30)
        session.send(b'x')
        session.await_text(rb'TERMINAL_CONTROL_DROP_OK\n', timeout=30)
        session.await_text(rb'hyper-sh\$ ', timeout=30)
        # No explicit mode restore: Process termination must restore the parent.
        session.send(b'echo RESTORE_X\x7fOK\r')
        session.await_text(rb'(?m)^RESTORE_OK\n', timeout=30)
        session.await_text(rb'hyper-sh\$ ', timeout=30)
        # The unread burst crosses shell raw input into cat's canonical input.
        session.send(b'cat\rterminal-typeahead\r\x04echo AFTER_CAT\r')
        session.await_text(rb'(?m)^terminal-typeahead\n', timeout=30)
        session.await_text(rb'(?m)^AFTER_CAT\n', timeout=30)
        session.await_text(rb'hyper-sh\$ ', timeout=30)
        for index in range(8):
            session.send(f'echo QUICK_{index}\r'.encode())
            session.await_text(f'(?m)^QUICK_{index}\\n'.encode(), timeout=30)
            session.await_text(rb'hyper-sh\$ ', timeout=30)
        session.send(b'exit\r')
        session.await_text(rb'HypeR session: console ready\n', timeout=30)
        session.await_text(rb'hyper-sh\$ ', timeout=30)
        command('metadata', rb'TERMINAL_METADATA_OK\n')
        session.send(b'echo FIRST\recho SECOND\r')
        session.await_text(rb'(?m)^FIRST\n', timeout=30)
        session.await_text(rb'(?m)^SECOND\n', timeout=30)
        session.await_text(rb'hyper-sh\$ ', timeout=30)
    print('verified Native terminal modes, timing, reply backpressure and foreground recovery')


if __name__ == '__main__':
    main()
