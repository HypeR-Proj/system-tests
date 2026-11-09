// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! The working set exceeds the mount's 256 canonical mutable-page budget.

use super::{Directory, FileRights, HandleRef, PAGE, Path, Result, SyncScope, VmarObject};

pub(super) fn run(
    directory: &Directory,
    root_vmar: HandleRef<'_, VmarObject>,
    root: &Path,
) -> Result {
    const PAGES: usize = 512;
    let path = root.join("f2fs-shared-pressure");
    let mut expected = vec![0u8; PAGES * PAGE];
    std::fs::write(&path, &expected)?;
    let file = super::native_open(directory, &path, FileRights::READ.union(FileRights::WRITE))?;
    let mapping = file.map_shared(root_vmar, super::options(0, PAGES * PAGE, true))?;
    for index in 0..PAGES {
        let byte = (index % 251 + 1) as u8;
        expected[index * PAGE] = byte;
        super::put(&mapping, index * PAGE, &[byte])?;
    }
    // Revisiting the complete working set requires refill after acknowledged
    // withdrawal, including pages that were dirty when capacity was exhausted.
    for index in 0..PAGES {
        super::check(&mapping, index * PAGE, &[expected[index * PAGE]])?;
    }
    mapping.sync(SyncScope::Full)?;
    if std::fs::read(&path)? != expected {
        return Err("shared-page eviction/refill changed canonical file content".into());
    }
    mapping
        .try_close()
        .map_err(|e| format!("pressure unmap: {e:?}"))?;
    drop(file);
    crate::remove_verified_fixture(&path).map_err(|e| format!("pressure fixture cleanup: {e}"))?;
    println!("F2FS-SMOKE: 512-page shared eviction/refill PASS");
    Ok(())
}
