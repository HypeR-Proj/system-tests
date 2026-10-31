// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

#[cfg(all(target_os = "hyper", target_arch = "riscv64"))]
mod runtime;

#[cfg(all(target_os = "hyper", target_arch = "aarch64"))]
mod mmio;

#[cfg(all(target_os = "hyper", target_arch = "aarch64"))]
mod io;

fn main() -> std::process::ExitCode {
    #[cfg(all(target_os = "hyper", target_arch = "riscv64"))]
    return runtime::main();
    #[cfg(all(target_os = "hyper", target_arch = "aarch64"))]
    return mmio::main();
    #[cfg(not(all(
        target_os = "hyper",
        any(target_arch = "riscv64", target_arch = "aarch64")
    )))]
    {
        eprintln!("vm-smoke requires an AArch64 or RISC-V HypeR /init bootstrap");
        std::process::ExitCode::FAILURE
    }
}
