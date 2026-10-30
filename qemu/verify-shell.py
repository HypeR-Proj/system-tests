#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Formal shell evaluation, descriptor ownership, and editing through Native I/O."""
import re
import sys

from hyper_test.guest_console import append_console_output
from hyper_test.session import Session, native_command
from hyper_test.command import Shell


def main():
    qemu, image, initramfs, logfile = sys.argv[1:]
    with Session(native_command(qemu, image, initramfs), logfile,
                 output_filter=append_console_output) as session:
        session.await_text(rb'HypeR session: console ready\n')
        session.await_text(rb'hyper-sh\$ ')

        shell = Shell(session)

        def run(command, expected=None, timeout=60):
            output = shell.check(command, timeout=timeout)
            if expected is not None and not re.search(expected, output):
                raise AssertionError(f'{command}: {output!r}')

        run("set --; argc() { echo ARGC=$#; }; argc \"$@\"; argc ''\"$@\"",
            rb'\nARGC=0\nARGC=1\n')
        run('false; echo STATUS=$?', rb'\nSTATUS=1\n')
        run('absent-shell-command; echo STATUS=$?', rb'\nSTATUS=127\n')
        run('if false; then echo BAD; elif true; then echo IF_OK; fi', rb'\nIF_OK\n')
        run('i=0; while [ $i -lt 3 ]; do i=$((i+1)); done; echo LOOP=$i', rb'\nLOOP=3\n')
        run('f() { local x=inner; echo "$1:$x"; return 7; }; x=outer; f arg; echo "$?:$x"',
            rb'\narg:inner\n7:outer\n')
        run('echo "SUB=$(printf \'a\\nb\\n\')"', rb'\nSUB=a\nb\n')
        run('printf \'first\\nsecond\\n\' > /tmp/shell-input; cat < /tmp/shell-input | grep second',
            rb'\nsecond\n')
        run('false | true; echo "PIPE=$? ${PIPESTATUS[@]}"', rb'\nPIPE=0 1 0\n')
        run('set -o pipefail; false | true; echo STATUS=$?; set +o pipefail', rb'\nSTATUS=1\n')
        run('f piped | cat; echo "FUNCTION_PIPE=$? ${PIPESTATUS[@]}"',
            rb'\npiped:inner\nFUNCTION_PIPE=0 7 0\n')
        run("sh -c 'echo out; echo err >&2' 2>&1 | cat", rb'\nout\nerr\n')
        run('cat /missing-shell-file 2>/tmp/shell-error; test -s /tmp/shell-error && echo REDIRECT_OK',
            rb'\nREDIRECT_OK\n')
        run('read -r line <<END\nhere $x\nEND\necho "$line"', rb'\nhere outer\n')
        run("read -r line <<'END'\nliteral $x\nEND\necho \"$line\"", rb'\nliteral \$x\n')
        run("cat <<'END' >/tmp/shell-script\necho \"SCRIPT:$1\"\nexit 9\nEND\nsh /tmp/shell-script arg; echo SCRIPT_STATUS=$?",
            rb'\nSCRIPT:arg\nSCRIPT_STATUS=9\n')
        run('exec 3>/tmp/shell-extra; echo EXTRA >&3 | cat; exec 3>&-; cat /tmp/shell-extra', rb'\nEXTRA\n')
        run('exec 3</tmp/shell-input; read -u 3 a; read -u 3 b; exec 3<&-; echo "$a:$b"', rb'\nfirst:second\n')
        # An external program that never reads stdin must not advance fd 3.
        run('exec 3</tmp/shell-input; /bin/echo NO_READ <&3; read -u 3 a; exec 3<&-; echo PRESERVED=$a',
            rb'\nNO_READ\nPRESERVED=first\n')
        # The same property holds across a nested shell pipeline worker.
        run('exec 3</tmp/shell-input; { /bin/echo WORKER <&3; } | cat; read -u 3 a; exec 3<&-; echo PRESERVED=$a',
            rb'\nWORKER\nPRESERVED=first\n')
        run("sh -c 'read -r line; echo READ=$line' < /tmp/shell-input", rb'\nREAD=first\n')
        run("sh -c 'echo MERGED >&2' |& cat", rb'\nMERGED\n')
        session.send(b'read -p "Name: " name; echo NAME=$name\r')
        session.await_text(rb'\nName: ')
        session.send(b'Ada\r')
        session.await_text(rb'\nNAME=Ada\nhyper-sh\$ ')
        session.send(b'exec 3<&0; { read -u 3 -p "Alias: " value; echo ALIAS=$value; } | cat; exec 3<&-\r')
        session.await_text(rb'\nAlias: ')
        session.send(b'input\r')
        session.await_text(rb'\nALIAS=input\nhyper-sh\$ ')
        # More than one pipe buffer, with a downstream command that exits early.
        run("{ for n in 1 2 3 4 5 6 7 8 9; do printf '%4096s\\n' x; done; } | grep -q x; echo EARLY=$?",
            rb'\nEARLY=0\n')
        run('if exec 3<<<branch; then read -u 3 value; echo BRANCH=$value; fi; exec 3<&-',
            rb'\nBRANCH=branch\n')
        run("IFS=: read -r a b c <<< ':middle::tail:'; printf '<%s><%s><%s>\\n' \"$a\" \"$b\" \"$c\"",
            rb'\n<><middle><:tail:>\n')
        # All 32 workers sleep together before input arrives. Their descriptor
        # servers plus the redirected output exceed wait-many's 64 items. The
        # final worker owns terminal control but reads a pipe: it must not
        # change terminal settings and cancel the first worker's stdin read.
        stages = ['{ read -p "PIPE_BOUNDARY: " value; echo "$value"; }']
        stages += ['{ read value; echo "$value"; }'] * 31
        session.send(('exec 3<<<held; { ' + ' | '.join(stages)
                      + '; } >/tmp/shell-long-pipe; cat /tmp/shell-long-pipe; exec 3<&-\r').encode())
        session.await_text(rb'\nPIPE_BOUNDARY: ', timeout=120)
        session.send(b'LONG_PIPE_OK\r')
        result = session.await_text(
            rb'\nLONG_PIPE_OK\nhyper-sh\$ |\nsh: operation interrupted\n', timeout=120)
        if b'sh: operation interrupted' in result:
            raise AssertionError('pipeline worker cancelled the shared terminal reader')
        run('scope() { true | { local value=inner; return 7; }; echo RETURN=$?; }; scope',
            rb'\nRETURN=7\n')
        run('printf abc > /tmp/glob-alpha; printf def > /tmp/glob-beta; echo /tmp/glob-*',
            rb'\n/tmp/glob-alpha /tmp/glob-beta\n')

        # From here the input line/history/terminal itself is under test. A
        # capture wrapper would change the history being asserted and hide UI.
        def run(command, expected=None, timeout=30):
            source = '{ ' + command + '\n}' if '\n' in command else command
            session.send(source.encode() + b'\r')
            output = session.await_text(rb'hyper-sh\$ ', timeout=timeout)
            if expected is not None and not re.search(expected, output):
                raise AssertionError(f'{command}: {output!r}')

        session.send(b"prin\t'%s\\n' COMPLETED\r")
        session.await_text(rb'\nCOMPLETED\nhyper-sh\$ ')
        session.send(b'echo edtor\x1b[D\x1b[D\x1b[Di\x1b[F\r')
        session.await_text(rb'\neditor\nhyper-sh\$ ')
        for backspace in (b'\x08', b'\x7f'):
            session.send(b'echo BACKSPACE_OKx')
            session.await_text(rb'echo BACKSPACE_OKx')
            session.send(backspace)
            erased = session.await_text(rb'\x08 \x08')
            if b'\x1b[J' in erased or b'hyper-sh$ ' in erased:
                raise AssertionError('tail backspace redrew the whole command line')
            session.send(b'\r')
            session.await_text(rb'\nBACKSPACE_OK\nhyper-sh\$ ')
        session.send(b'echo edxitor\x1b[D\x1b[D\x1b[D\x1b[D\x7f\x1b[F\r')
        session.await_text(rb'\neditor\nhyper-sh\$ ')
        run("printf 'SPACE_FILE\\n' > '/tmp/review spaced'")
        session.send(b'cat /tmp/review\t\r')
        session.await_text(rb'\nSPACE_FILE\nhyper-sh\$ ')
        run('mkdir /tmp/complete; mkdir /tmp/complete/alpha; mkdir /tmp/complete/beta; touch /tmp/complete/file',
            rb'hyper-sh')
        session.send(b'cat /tmp/complete/\t')
        choices = session.await_text(rb'\nalpha/  beta/  file  \n')
        if b'\n/tmp/complete/alpha/' in choices:
            raise AssertionError('completion repeated the parent path in candidate labels')
        session.send(b'\x03')
        session.await_text(rb'\^C\nhyper-sh\$ ')
        session.send(b'cd /tmp/complete/\t')
        session.await_text(rb'\nalpha/  beta/  \n')
        session.send(b'\x03')
        session.await_text(rb'\^C\nhyper-sh\$ ')
        # A wide character cannot fit in the last column; Home/End must return
        # to the same logical line after that wrap, and Backspace deletes Z.
        session.send(b'echo ' + b'x' * 64 + '界Z'.encode() + b'\x01\x05\x7f\r')
        session.await_text(b'\n' + b'x' * 64 + '界'.encode() + rb'\nhyper-sh\$ ')
        session.send(b'if true; then\r')
        session.await_text(rb'> ')
        session.send(b'echo MUST_NOT_RUN\x03')
        cancelled = session.await_text(rb'\^C\nhyper-sh\$ ')
        if b'\nMUST_NOT_RUN\n' in cancelled:
            raise AssertionError('cancelled command executed')
        run('echo CANCEL=$?', rb'\nCANCEL=130\n')
        run("echo 'echo STDIN_SCRIPT' | sh", rb'\nSTDIN_SCRIPT\n')
        run("cat <<'END' >/tmp/shell-order\nfor n in 1 2 3 4; do\nsh -c 'printf ERROR_ >&2; printf \"TAIL\\n\" >&2; echo OUTPUT'\ndone\nEND\nprintf '' | sh /tmp/shell-order",
            rb'\n(?:ERROR_TAIL\nOUTPUT\n){4}')
        # Bracketed paste never executes embedded newlines before Enter.
        session.send(b'\x1b[200~echo PASTE_ONE\necho PASTE_TWO\x1b[201~')
        session.pump(0.1)
        if b'\nPASTE_ONE\n' in session.pending:
            raise AssertionError('paste executed before Enter')
        session.send(b'\r')
        session.await_text(rb'\nPASTE_ONE\nPASTE_TWO\nhyper-sh\$ ')
        run('echo VOLATILE_HISTORY', rb'\nVOLATILE_HISTORY\n')
        session.send(b'\x12VOLATILE_HISTORY\r\r')
        session.await_text(rb'\nVOLATILE_HISTORY\nhyper-sh\$ ')
        run("history | grep 'echo VOLATILE_HISTORY'", rb'\n\s+\d+  echo VOLATILE_HISTORY\n')
        run('test -s /tmp/hyper-sh.history && echo HISTORY_FILE_OK', rb'\nHISTORY_FILE_OK\n')
        session.send(b'exit\r')
        session.await_text(rb'HypeR session: console ready\n')
        session.await_text(rb'hyper-sh\$ ')
        run('history', rb'echo VOLATILE_HISTORY')
        run('history -c; echo HISTORY_CLEARED', rb'\nHISTORY_CLEARED\n')
        print('Native shell syntax, pipelines, descriptors, history and editing passed', flush=True)


if __name__ == '__main__':
    main()
