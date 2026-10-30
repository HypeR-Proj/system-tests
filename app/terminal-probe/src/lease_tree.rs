// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Revoke an ancestor while its foreground descendant uses peer flow control.

use hyper_os::fs::FileRights;
use hyper_os::handle::{OwnedHandle, ProcessObject, Rights, RightsOffer};
use hyper_os::startup::{self, Startup};
use hyper_os::task::ProcessBuilder;
use hyper_os::wait::wait_many;
use hyper_terminal::{EventFlags, Settings, Terminal};

use crate::Result;

pub(super) fn run(startup: &mut Startup<'_>, parent: &mut Terminal) -> Result<()> {
    let owner = start_owner(startup)?;
    let child = parent.prepare_child()?;
    let child_id = child.id;
    parent.bind_child(child_id, owner.0.as_process_supervisor().observe()?)?;
    let mut child_terminal = Terminal::from_handle(child.control);
    let grandchild = child_terminal.prepare_child()?;
    child_terminal.bind_child(grandchild.id, owner.0.as_process_supervisor().observe()?)?;
    let mut grandchild_terminal = Terminal::from_handle(grandchild.control);
    let mut settings = Settings::raw();
    settings.input.send_flow_control = true;
    grandchild_terminal.set_settings(settings)?;
    parent.take_events()?;
    parent.release_child(child_id)?;
    // Restoring flow settings is asynchronous: wait for its foreground event,
    // then prove authority returned to the surviving parent, not the removed
    // intermediate child. The endpoint is only a wake hint: unrelated events
    // and hints whose flags were already consumed cannot grant authority.
    let deadline = hyper_os::time::deadline_after(std::time::Duration::from_secs(5))?.as_raw();
    while parent.take_events()?.0 & EventFlags::FOREGROUND_CHANGED == 0 {
        let event = parent.event_wait_item().ok_or("missing terminal events")?;
        wait_many(&[event], deadline)?;
    }
    parent
        .set_size(80, 24)
        .map_err(|error| format!("restore parent foreground: {error}"))?;
    Ok(())
}

fn start_owner(startup: &mut Startup<'_>) -> Result<StopOnDrop> {
    let root = startup.take_root_directory()?;
    let executable = root.open(
        "/bin/terminal-probe",
        FileRights::READ
            .union(FileRights::EXECUTE)
            .union(FileRights::PROTECT)
            .union(FileRights::DUPLICATE)
            .union(FileRights::TRANSFER),
    )?;
    let builder = ProcessBuilder::create(
        startup.borrow(startup::TASK_FACTORY)?,
        startup.borrow(startup::TASK_GROUP)?,
        startup.borrow(startup::RESOURCE_DOMAIN)?,
        executable.as_handle_ref(),
        startup.borrow(startup::CHILD_LOADER)?,
    )?;
    builder.set_name("terminal-lease-owner")?;
    builder.add_argument("terminal-probe")?;
    builder.add_argument("lease-owner")?;
    builder.add_handle_duplicate(
        startup.borrow(startup::DYNAMIC_LIBRARY_DIRECTORY)?,
        startup::DYNAMIC_LIBRARY_DIRECTORY.as_raw(),
        RightsOffer::Exact(Rights::READ.union(Rights::EXECUTE).union(Rights::PROTECT)),
    )?;
    builder.seal()?;
    Ok(StopOnDrop(builder.start().map_err(|error| error.error())?))
}

struct StopOnDrop(OwnedHandle<ProcessObject>);
impl Drop for StopOnDrop {
    fn drop(&mut self) {
        let _ = self.0.as_process_supervisor().request_stop();
    }
}
