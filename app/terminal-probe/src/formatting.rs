// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

use std::io::{self, Read, Write};

use hyper_terminal::{Settings, Terminal};

use crate::{Result, ready, require};

pub(super) fn run(terminal: &mut Terminal) -> Result<()> {
    let mut settings = Settings::default();
    terminal.set_settings(settings)?;
    ready("EDIT")?;
    check_line(b"foo/\nX\n")?;

    settings.visual_erase = false;
    terminal.set_settings(settings)?;
    ready("ERASE_ECHO")?;
    check_line(b"z\n")?;

    settings.echo_erased = true;
    terminal.set_settings(settings)?;
    ready("HARDCOPY")?;
    check_line(b"ac\n")?;

    settings.output.uppercase = true;
    terminal.set_settings(settings)?;
    require(
        terminal.settings()? == settings,
        "formatting settings roundtrip",
    )?;
    io::stdout().write_all(b"terminal_formatting_ok\n")?;
    io::stdout().flush()?;
    Ok(())
}

fn check_line(expected: &[u8]) -> Result<()> {
    let mut bytes = [0; 32];
    let count = io::stdin().read(&mut bytes)?;
    require(&bytes[..count] == expected, "formatted input record")
}
