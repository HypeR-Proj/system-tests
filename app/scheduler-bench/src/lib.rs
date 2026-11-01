// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

//! Bounded measurement configuration, distributions and absolute-period accounting.
pub mod config;
#[cfg(target_os = "hyper")]
pub mod native;
pub mod stats;

#[cfg(target_os = "hyper")]
extern crate hyper_clap_shared as clap;
