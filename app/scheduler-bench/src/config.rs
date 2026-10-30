// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

use clap::{Args, Parser, Subcommand, ValueEnum};
use serde::{Deserialize, Serialize};

#[derive(Parser)]
#[command(
    name = "sched-bench",
    about = "Native scheduler latency and contention benchmarks"
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}
#[derive(Subcommand)]
pub enum Command {
    /// Run all cases, including same-CPU and cross-CPU channel handoff.
    Suite(Options),
    /// Consecutive monotonic observations: measurement floor, not timer resolution.
    Clock(Options),
    /// Absolute periodic wakeup lateness, including missed periods.
    Cyclic(Options),
    /// Elapsed yield-call latency with one or more runnable competitors.
    Yield(Options),
    /// Same-process atomic wait/wake handoff: one-way latency and round trip.
    Atomic(Options),
    /// Process-to-process channel handoff: same CPU or --cross-cpu.
    Channel(Options),
    /// Equal-work throughput and progress fairness under CPU contention.
    Fairness(Options),
    /// Thread creation to entry latency; joins occur outside each sample.
    Spawn(Options),
    #[command(hide = true)]
    Worker { config: String },
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, ValueEnum, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Case {
    Clock,
    Cyclic,
    Yield,
    Atomic,
    Channel,
    Fairness,
    Spawn,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, ValueEnum, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Overrun {
    Skip,
    CatchUp,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, ValueEnum, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Load {
    Cpu,
    Memory,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, ValueEnum, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Policy {
    Inherit,
    Fair,
    Fifo,
    Budget,
}

#[derive(Args, Clone, Debug, Serialize, Deserialize)]
pub struct Options {
    /// Comma-separated CPUs/ranges, or inherit. Each worker gets a singleton mask.
    #[arg(long, default_value = "inherit")]
    pub cpus: String,
    #[arg(long, default_value_t = 1000)]
    pub duration_ms: u64,
    /// Stop after this many iterations too; zero uses only the duration bound.
    #[arg(long, default_value_t = 0)]
    pub iterations: u64,
    #[arg(long, default_value_t = 1)]
    pub repeat: usize,
    #[arg(long, default_value_t = 1000)]
    pub interval_us: u64,
    #[arg(long, default_value_t = 20)]
    pub warmup: u64,
    /// Runnable measured threads for yield/fairness; cyclic has one per worker.
    #[arg(long, default_value_t = 2)]
    pub threads: usize,
    #[arg(long, value_enum, default_value_t = Overrun::Skip)]
    pub overrun: Overrun,
    #[arg(long, default_value_t = 1000)]
    pub histogram_bin_ns: u64,
    #[arg(long, default_value_t = 32768)]
    pub histogram_bins: usize,
    /// Retain samples in JSON, bounded by --max-raw per measured series.
    #[arg(long)]
    pub raw: bool,
    #[arg(long, default_value_t = 100000)]
    pub max_raw: usize,
    #[arg(long, default_value_t = 0)]
    pub load_threads: usize,
    #[arg(long, value_enum, default_value_t = Load::Cpu)]
    pub load: Load,
    #[arg(long, default_value_t = 8)]
    pub load_mib: usize,
    #[arg(long)]
    pub cross_cpu: bool,
    /// Requires already-delegated Native scheduling rights; no silent fallback.
    #[arg(long, value_enum, default_value_t = Policy::Inherit)]
    pub policy: Policy,
    #[arg(long)]
    pub band: Option<u8>,
    #[arg(long, default_value_t = 128)]
    pub weight: u16,
    #[arg(long, default_value_t = 80)]
    pub fifo_priority: u8,
    #[arg(long, default_value_t = 10000)]
    pub quantum_us: u64,
    #[arg(long, default_value_t = 5000)]
    pub budget_us: u64,
    #[arg(long, default_value_t = 10000)]
    pub period_us: u64,
    /// Populate/lock current mappings after preparing buffers/stacks; capability required.
    #[arg(long)]
    pub lock_memory: bool,
    #[arg(long, default_value_t = 30000)]
    pub timeout_ms: u64,
    /// Explicit executable path for deployments outside /bin.
    #[arg(long, default_value = "/bin/sched-bench")]
    pub worker_image: String,
    #[arg(long)]
    pub output: Option<String>,
    #[arg(long)]
    pub json: bool,
}

impl Options {
    pub fn validate(&self) -> Result<(), String> {
        let cpus = cpu_list(&self.cpus)?;
        if !(1..=3_600_000).contains(&self.duration_ms)
            || !(1..=100).contains(&self.repeat)
            || !(1..=1_000_000_000).contains(&self.interval_us)
            || self.interval_us > self.duration_ms * 1000
            || self.warmup > 10000
            || !(1..=64).contains(&self.threads)
            || self.load_threads > 64
            || !(1..=1_000_000_000).contains(&self.histogram_bin_ns)
            || !(1..=1_000_000).contains(&self.histogram_bins)
            || self.max_raw > 1_000_000
            || !(1..=1024).contains(&self.load_mib)
            || !(100..=3_600_000).contains(&self.timeout_ms)
            || self.iterations > 1_000_000_000
            || self.worker_image.is_empty()
        {
            return Err("invalid bounds; use --help for units (duration up to 1 hour, at most 64 workers/threads)".into());
        }
        if self.weight == 0
            || self.weight > 1024
            || self.quantum_us == 0
            || self.quantum_us > 1_000_000
            || self.period_us == 0
            || self.period_us > 1_000_000
            || self.budget_us == 0
            || self.budget_us > self.period_us
        {
            return Err("invalid scheduling parameters".into());
        }
        if (self.weight != 128 || self.quantum_us != 10000) && self.policy != Policy::Fair {
            return Err("--weight/--quantum-us require --policy fair".into());
        }
        if self.fifo_priority != 80 && self.policy != Policy::Fifo {
            return Err("--fifo-priority requires --policy fifo".into());
        }
        if (self.budget_us != 5000 || self.period_us != 10000) && self.policy != Policy::Budget {
            return Err("--budget-us/--period-us require --policy budget".into());
        }
        // Bound even a whole suite's largest concurrent case, not merely one histogram.
        let series = cpus
            .len()
            .checked_mul(self.threads.max(2))
            .ok_or("series overflow")?;
        let bytes = (self.histogram_bins as u128 * 8
            + if self.raw {
                self.max_raw as u128 * 24
            } else {
                0
            })
            * series as u128;
        if bytes > 128 * 1024 * 1024 {
            return Err("measurement buffers exceed 128 MiB; reduce threads/bins/max-raw".into());
        }
        // A channel pair has two independently prepared load groups per CPU.
        if cpus.len() as u128 * 2 * self.load_threads as u128 * self.load_mib as u128 > 256
            && self.load == Load::Memory
        {
            return Err("memory background load exceeds 256 MiB".into());
        }
        Ok(())
    }
    pub fn validate_retained(&self, series: usize) -> Result<(), String> {
        let bytes = (self.histogram_bins as u128 * 8
            + if self.raw {
                self.max_raw as u128 * 24
            } else {
                0
            })
            * series as u128
            * self.repeat as u128;
        if bytes > 256 * 1024 * 1024 {
            return Err(
                "retained suite results exceed 256 MiB; reduce repeat/CPUs/threads/bins/max-raw"
                    .into(),
            );
        }
        Ok(())
    }
}

/// An empty affinity is never invented: inherit is represented separately.
pub fn cpu_list(input: &str) -> Result<Vec<Option<u16>>, String> {
    if input == "inherit" {
        return Ok(vec![None]);
    }
    let mut cpus = Vec::new();
    for part in input.split(',') {
        let (lo, hi) = if let Some((a, b)) = part.split_once('-') {
            (
                a.parse::<u16>().map_err(|_| "invalid CPU range")?,
                b.parse::<u16>().map_err(|_| "invalid CPU range")?,
            )
        } else {
            let n = part.parse::<u16>().map_err(|_| "invalid CPU")?;
            (n, n)
        };
        if lo > hi || hi >= 256 {
            return Err("CPU indices must be 0..255, with ascending ranges".into());
        }
        for cpu in lo..=hi {
            if cpus.contains(&Some(cpu)) {
                return Err("duplicate CPU".into());
            }
            cpus.push(Some(cpu));
        }
    }
    if cpus.is_empty() || cpus.len() > 64 {
        return Err("select 1..64 CPUs".into());
    }
    Ok(cpus)
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Job {
    pub case: Case,
    pub options: Options,
    pub cpu: Option<u16>,
    /// Channel jobs use one producer and one receiver process.
    pub receiver: bool,
    pub peer_cpu: Option<u16>,
    pub repetition: usize,
}

/// Absolute phase accounting. Skip reports missed releases instead of fabricating
/// immediate wakeups for already missed periods. Catch-up is explicitly opt-in.
pub fn next_release(
    target: u64,
    observed: u64,
    interval: u64,
    policy: Overrun,
) -> Option<(u64, u64)> {
    if interval == 0 || observed < target {
        return None;
    }
    let skipped = if policy == Overrun::Skip {
        (observed - target) / interval
    } else {
        0
    };
    let next = target.checked_add(skipped.checked_add(1)?.checked_mul(interval)?)?;
    (next != u64::MAX).then_some((next, skipped))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn options(arguments: &[&str]) -> Result<Options, String> {
        let cli = Cli::try_parse_from(
            ["sched-bench", "cyclic"]
                .into_iter()
                .chain(arguments.iter().copied()),
        )
        .map_err(|e| e.to_string())?;
        match cli.command {
            Command::Cyclic(o) => Ok(o),
            _ => Err("wrong parser command".into()),
        }
    }
    #[test]
    fn invalid_policy_and_memory_requests_fail_before_spawning() -> Result<(), String> {
        options(&[])?.validate()?;
        for arguments in [
            vec!["--interval-us", "0"],
            vec!["--duration-ms", "0"],
            vec!["--weight", "42"],
            vec!["--policy", "budget", "--budget-us", "10001"],
            vec!["--cpus", "0-63", "--histogram-bins", "1000000"],
            vec![
                "--load",
                "memory",
                "--load-threads",
                "64",
                "--load-mib",
                "8",
            ],
        ] {
            assert!(options(&arguments)?.validate().is_err(), "{arguments:?}");
        }
        let o = options(&["--repeat", "100"])?;
        assert!(o.validate_retained(100).is_err());
        assert!(o.validate_retained(1).is_ok());
        Ok(())
    }
    #[test]
    fn skip_deadlines_are_always_future_and_keep_the_original_phase() {
        for interval in [1, 7, 1000, 1_000_000] {
            for delay in [0, 1, 6, 1000, 10_000_000] {
                let target = 173;
                let observed = target + delay;
                let result = next_release(target, observed, interval, Overrun::Skip);
                assert_eq!(
                    result,
                    Some((target + (delay / interval + 1) * interval, delay / interval))
                );
                if let Some((next, _)) = result {
                    assert!(next > observed && next - observed <= interval);
                }
            }
        }
    }
    #[test]
    fn cpus_are_nonempty_unique_and_bounded() {
        assert_eq!(cpu_list("1,3-4"), Ok(vec![Some(1), Some(3), Some(4)]));
        for text in ["", "1,1", "3-1", "256", "-1", "0-65"] {
            assert!(cpu_list(text).is_err(), "{text}");
        }
    }
    #[test]
    fn deadlines_keep_phase_and_account_exact_boundaries() {
        assert_eq!(next_release(100, 100, 10, Overrun::Skip), Some((110, 0)));
        assert_eq!(next_release(100, 110, 10, Overrun::Skip), Some((120, 1)));
        assert_eq!(next_release(100, 137, 10, Overrun::Skip), Some((140, 3)));
        assert_eq!(next_release(100, 137, 10, Overrun::CatchUp), Some((110, 0)));
        assert_eq!(
            next_release(u64::MAX - 10, u64::MAX - 10, 10, Overrun::Skip),
            None
        );
        assert_eq!(next_release(100, 99, 10, Overrun::Skip), None);
    }
}
