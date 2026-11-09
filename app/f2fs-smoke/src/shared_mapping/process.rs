// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Two independent address spaces share frames while the parent fences them.

use super::{FileRights, LENGTH, NAME, PAGE, Path, Result, SharedFileMapping, SyncScope};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

const CONTROL: usize = PAGE - 64;
const READY: usize = 0;
const START: usize = 1;
const DONE: usize = 2;
const STOP: usize = 3;
const COUNT: usize = 4;

fn word(mapping: &SharedFileMapping, offset: usize) -> Result<&AtomicU32> {
    if !offset.is_multiple_of(4) || offset.checked_add(4).is_none_or(|end| end > mapping.len()) {
        return Err("unaligned shared control word".into());
    }
    // SAFETY: the complete aligned word belongs to mapping. During this
    // protocol every application access uses AtomicU32, including both processes.
    Ok(unsafe { AtomicU32::from_ptr(mapping.as_mut_ptr()?.add(offset).cast()) })
}
fn control(mapping: &SharedFileMapping, index: usize) -> Result<&AtomicU32> {
    word(mapping, CONTROL + index * 4)
}
fn await_flag(mapping: &SharedFileMapping, index: usize) -> Result {
    let flag = control(mapping, index)?;
    let started = Instant::now();
    while flag.load(Ordering::Acquire) == 0 {
        if started.elapsed() > Duration::from_secs(15) {
            return Err(format!("shared child handshake {index} timed out").into());
        }
        std::thread::yield_now();
    }
    Ok(())
}

struct Child(Option<std::process::Child>);
impl Child {
    fn join(mut self) -> Result {
        let started = Instant::now();
        loop {
            let child = self.0.as_mut().ok_or("shared child lost ownership")?;
            if let Some(status) = child.try_wait()? {
                self.0.take();
                return if status.success() {
                    Ok(())
                } else {
                    Err(format!("shared child failed: {status}").into())
                };
            }
            if started.elapsed() > Duration::from_secs(15) {
                return Err("shared child exit timed out".into());
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    }
}
impl Drop for Child {
    fn drop(&mut self) {
        if let Some(child) = self.0.as_mut() {
            // Early sync/handshake failures cannot leave a writer behind. The
            // runtime kill operation requests terminal stop, then wait joins it.
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

pub(super) fn run(root: &Path, mapping: &SharedFileMapping) -> Result {
    for index in 0..5 {
        control(mapping, index)?.store(0, Ordering::Relaxed);
    }
    let child = Child(Some(
        std::process::Command::new("/bin/f2fs-smoke")
            .arg("shared-child")
            .arg(root)
            .arg(format!("{}", mapping.as_ptr() as usize))
            .spawn()?,
    ));
    let outcome = (|| -> Result {
        await_flag(mapping, READY)?;
        control(mapping, START)?.store(1, Ordering::Release);
        await_flag(mapping, COUNT)?;
        for _ in 0..8 {
            mapping.sync(SyncScope::Data)?;
        }
        Ok(())
    })();
    control(mapping, STOP)?.store(1, Ordering::Release);
    // join/Drop finishes before any restoration or owner teardown, including
    // when a fence failed. Avoid concurrent non-atomic observations entirely.
    let joined = child.join();
    outcome?;
    joined?;
    if control(mapping, DONE)?.load(Ordering::Acquire) != 1
        || control(mapping, COUNT)?.load(Ordering::Acquire) == 0
        || word(mapping, 2 * PAGE)?.load(Ordering::Acquire) & 0xffff_0000 != 0x9090_0000
    {
        return Err("cross-process mapped writes were not coherent".into());
    }
    for index in 0..5 {
        control(mapping, index)?.store(0x3131_3131, Ordering::Relaxed);
    }
    // Restore the fixture only after the child stopped; later syscall-copy
    // coverage overwrites the full third page with its own expected pattern.
    word(mapping, 2 * PAGE)?.store(0, Ordering::Release);
    mapping.sync(SyncScope::Data)?;
    println!("F2FS-SMOKE: cross-process aliases and concurrent freeze PASS");
    Ok(())
}

pub fn child(root: &Path) -> Result {
    let parent_address: usize = std::env::args()
        .nth(3)
        .ok_or("missing parent address")?
        .parse()?;
    let mut startup = hyper_rt::process::startup()?;
    let directory = startup.take_root_directory()?;
    let root_vmar = startup.borrow(hyper_os::startup::ROOT_VMAR)?;
    let file = super::native_open(
        &directory,
        &root.join(NAME),
        FileRights::READ.union(FileRights::WRITE),
    )?;
    let first = file.map_shared(root_vmar, super::options(0, LENGTH, true))?;
    // Keeping a matching initial region reserved forces the second automatic
    // placement elsewhere, without guessing or replacing virtual addresses.
    let spare;
    let mapping = if first.as_ptr() as usize == parent_address {
        spare = Some(first);
        file.map_shared(root_vmar, super::options(0, LENGTH, true))?
    } else {
        spare = None;
        first
    };
    if mapping.as_ptr() as usize == parent_address {
        return Err("child aliases parent VA".into());
    }
    control(&mapping, READY)?.store(1, Ordering::Release);
    await_flag(&mapping, START)?;
    let started = Instant::now();
    let mut count = 0u32;
    while control(&mapping, STOP)?.load(Ordering::Acquire) == 0 {
        if started.elapsed() > Duration::from_secs(30) {
            return Err("parent did not stop mapped child".into());
        }
        count = count.wrapping_add(1);
        word(&mapping, 2 * PAGE)?.store(0x9090_0000 | (count & 0xffff), Ordering::Relaxed);
        control(&mapping, COUNT)?.store(count, Ordering::Release);
        std::thread::yield_now();
    }
    control(&mapping, DONE)?.store(1, Ordering::Release);
    mapping
        .try_close()
        .map_err(|e| format!("child unmap: {e:?}"))?;
    if let Some(spare) = spare {
        spare
            .try_close()
            .map_err(|e| format!("child spare unmap: {e:?}"))?;
    }
    Ok(())
}
