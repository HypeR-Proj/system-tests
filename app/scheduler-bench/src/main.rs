// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

use clap::Parser;
#[cfg(target_os = "hyper")]
extern crate hyper_clap_shared as _;
use hyper_scheduler_bench::config::Cli;
#[cfg(target_os = "hyper")]
use hyper_scheduler_bench::config::Command;
fn main() {
    let args = Cli::parse();
    #[cfg(target_os = "hyper")]
    let result = match args.command {
        Command::Worker { config } => hyper_scheduler_bench::native::worker(&config),
        command => hyper_scheduler_bench::native::run(command),
    };
    #[cfg(not(target_os = "hyper"))]
    let result: Result<(), String> = {
        let _ = args;
        Err("sched-bench executes only on HypeR Native; host builds provide configuration/statistics tests".into())
    };
    if let Err(error) = result {
        eprintln!("sched-bench: {error}");
        std::process::exit(1);
    }
}
