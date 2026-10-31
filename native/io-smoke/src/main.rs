// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Native /init fixture for a real physical-disk, two-Linux-VM deployment.

#[cfg(all(target_os = "hyper", target_arch = "aarch64"))]
mod runtime;

fn main() -> std::process::ExitCode {
    #[cfg(all(target_os = "hyper", target_arch = "aarch64"))]
    return runtime::main();
    #[cfg(not(all(target_os = "hyper", target_arch = "aarch64")))]
    {
        eprintln!("io-smoke requires the AArch64 HypeR /init bootstrap");
        std::process::ExitCode::FAILURE
    }
}
