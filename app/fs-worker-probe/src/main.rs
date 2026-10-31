// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Test-only mounted provider. It holds real volume ownership without touching
//! the media, and dies on demand without writing its own diagnostic message.

use hyper_filesystem::{
    Timestamp,
    protocol::{self, DirectoryCursor, Entry, Error},
};
use hyper_fs_service::Volume;

const FLEET: &[u8] = b"{\"format\":\"hyper.vm-config\",\"virtual-machines\":[]}";
const FILES: [&str; 4] = ["vms.json", "exit", "fault", "stop-fault"];

#[derive(Default)]
struct Probe {
    fault_on_stop: bool,
}

impl Volume for Probe {
    fn stat(&mut self, path: &str) -> Result<Entry, Error> {
        let mut entry = Entry::empty();
        if !path.is_empty() {
            if !FILES.contains(&path) {
                return Err(Error::Missing);
            }
            entry.name[..path.len()].copy_from_slice(path.as_bytes());
            entry.name_len = path.len();
            entry.kind = protocol::FILE;
            entry.size = if path == "vms.json" {
                FLEET.len() as u64
            } else {
                1
            };
        }
        Ok(entry)
    }

    fn read_directory(
        &mut self,
        path: &str,
        cursor: DirectoryCursor,
        output: &mut [u8],
    ) -> Result<usize, Error> {
        if !path.is_empty() {
            return Err(Error::Missing);
        }
        let mut batch =
            protocol::DirectoryBatchBuilder::new(output, cursor).ok_or(Error::InvalidInput)?;
        for (index, path) in FILES.iter().enumerate().skip(cursor.words[0] as usize) {
            let next = DirectoryCursor {
                words: [index as u64 + 1, 0, 0, 0],
            };
            if !batch
                .push(&self.stat(path)?, next)
                .ok_or(Error::InvalidInput)?
            {
                return batch.finish(false).ok_or(Error::InvalidInput);
            }
        }
        batch.finish(true).ok_or(Error::InvalidInput)
    }

    fn read(&mut self, path: &str, offset: u64, output: &mut [u8]) -> Result<usize, Error> {
        self.stat(path)?;
        let bytes = if path == "vms.json" { FLEET } else { b"x" };
        let start = offset.min(bytes.len() as u64) as usize;
        let count = output.len().min(bytes.len() - start);
        output[..count].copy_from_slice(&bytes[start..start + count]);
        Ok(count)
    }

    fn write(&mut self, path: &str, _offset: u64, input: &[u8]) -> Result<usize, Error> {
        match path {
            "exit" => std::process::exit(37),
            "fault" => fault(),
            "stop-fault" => {
                self.fault_on_stop = true;
                Ok(input.len())
            }
            _ => Err(Error::ReadOnly),
        }
    }
    fn create(&mut self, _path: &str, _directory: bool) -> Result<(), Error> {
        Err(Error::ReadOnly)
    }
    fn remove(&mut self, _path: &str) -> Result<(), Error> {
        Err(Error::ReadOnly)
    }
    fn rename(&mut self, _path: &str, _target: &str) -> Result<(), Error> {
        Err(Error::ReadOnly)
    }
    fn resize(&mut self, path: &str, _length: u64) -> Result<(), Error> {
        self.stat(path).map(|_| ())
    }
    fn sync(&mut self) -> Result<(), Error> {
        Ok(())
    }
    fn set_times(
        &mut self,
        _path: &str,
        _a: Option<Timestamp>,
        _m: Option<Timestamp>,
    ) -> Result<(), Error> {
        Ok(())
    }
}

fn fault() -> ! {
    // SAFETY: This isolated test process deliberately raises a user exception;
    // it holds no borrowed kernel memory and the test expects process retirement.
    unsafe {
        #[cfg(target_arch = "aarch64")]
        core::arch::asm!("brk #0", options(noreturn));
        #[cfg(target_arch = "riscv64")]
        core::arch::asm!("ebreak", options(noreturn));
        #[cfg(target_arch = "x86_64")]
        core::arch::asm!("ud2", options(noreturn));
    }
}

fn run() -> Result<(), String> {
    let mut startup = hyper_rt::process::startup().map_err(|e| format!("{e:?}"))?;
    let block = hyper_os::block::NativeBlock::from_handle(
        startup
            .take(hyper_service::filesystem::BLOCK)
            .map_err(|e| format!("{e:?}"))?,
    );
    let queue = block
        .create_queue(hyper_os::block::QueueConfig::default())
        .map_err(|e| format!("{e:?}"))?;
    let mut probe = Probe::default();
    let completed = hyper_fs_service::serve(&mut startup, &mut probe, queue)?;
    if probe.fault_on_stop {
        // Namespace detach succeeded, but the manager must retain the reason
        // when this process dies before returning its exclusive volume owner.
        fault();
    }
    hyper_fs_service::return_block(completed, block)
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("fs-worker-probe: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
