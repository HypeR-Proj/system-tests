// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Synthetic no-device test environment. Never installed in a product image.
//!
//! The test publisher supplies a CPIO file of formal programs. Copy it into a
//! distinct RAMFS mount before running the real stage-1 supervisor, so formal
//! mappings cannot retain the boot archive through a promoted subtree. Hardware
//! and board acceptance separately exercise independent block-backed storage.

#[cfg(target_os = "hyper")]
#[path = "native-init-payload.rs"]
mod payload;

#[cfg(target_os = "hyper")]
use std::process::ExitCode;

#[cfg(target_os = "hyper")]
fn main() -> ExitCode {
    let Ok(mut startup) = hyper_rt::process::startup() else {
        return ExitCode::FAILURE;
    };
    let prepared = (|| -> Result<(), Box<dyn std::error::Error>> {
        std::fs::create_dir_all("/system")?;
        let root = startup
            .borrow(hyper_os::startup::ROOT_DIRECTORY)
            .map_err(|error| format!("root authority: {error:?}"))?;
        hyper_os::filesystem::mount_ramfs(root, "/system")
            .map_err(|error| format!("independent RAMFS: {error:?}"))?;
        payload::unpack(std::fs::File::open("/test-system.cpio")?, "/system")?;
        for directory in ["boot", "tmp", "data"] {
            std::fs::create_dir_all(format!("/system/{directory}"))?;
        }
        Ok(())
    })();
    if let Err(error) = prepared {
        if let Ok(console) = startup.console() {
            let _ = console.write_all(format!("Native test init: {error}\n").as_bytes());
        }
        return ExitCode::FAILURE;
    }
    // The payload file and extraction buffers are gone before stage-1 takes the
    // startup capabilities. Only the independent mount owns the copied files.
    match hyper_bootstrap_stage1_init::runtime::run(&mut startup) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            use std::io::Write;
            let _ = write!(std::io::stderr(), "{}", error.diagnostic());
            ExitCode::FAILURE
        }
    }
}

// Host checks compile this target; only Native QEMU fixtures execute it.
#[cfg(not(target_os = "hyper"))]
fn main() {}
