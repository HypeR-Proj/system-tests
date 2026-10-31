# SPDX-FileCopyrightText: 2026 roolrz
# SPDX-License-Identifier: Apache-2.0

"""Corrupt one already admitted Native archive for runtime failure tests."""


def replace_file(path, name, contents):
    """Preserve newc metadata and all other files; never extract host paths."""
    source = path.read_bytes()
    output = bytearray()
    offset = 0
    found = False
    while offset < len(source):
        header = source[offset:offset + 110]
        if len(header) != 110 or header[:6] != b'070701':
            raise ValueError('invalid Native fixture archive')
        fields = [int(header[6 + index * 8:14 + index * 8], 16) for index in range(13)]
        namesize, size = fields[11], fields[6]
        name_end = offset + 110 + namesize
        start = (name_end + 3) & ~3
        end = start + size
        if not namesize or name_end > len(source) or end > len(source):
            raise ValueError('truncated Native fixture archive')
        encoded = source[offset + 110:name_end]
        if encoded[-1:] != b'\0':
            raise ValueError('unterminated Native fixture name')
        data = source[start:end]
        if encoded[:-1] == name.encode():
            if found or fields[1] & 0o170000 != 0o100000:
                raise ValueError('expected exactly one regular fixture file')
            found = True
            data = contents
            fields[6] = len(data)
        output.extend(b'070701' + ''.join(f'{value:08x}' for value in fields).encode())
        output.extend(encoded)
        output.extend(b'\0' * (-len(output) % 4))
        output.extend(data)
        output.extend(b'\0' * (-len(output) % 4))
        offset = (end + 3) & ~3
        if encoded == b'TRAILER!!!\0':
            break
    if not found:
        raise ValueError(f'fixture file is absent: {name}')
    path.write_bytes(output)
