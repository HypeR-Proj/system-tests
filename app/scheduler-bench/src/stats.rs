// SPDX-FileCopyrightText: 2026 roolrz
// SPDX-License-Identifier: Apache-2.0

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
pub struct Sample {
    pub sequence: u64,
    pub target_ns: u64,
    pub observed_ns: u64,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Distribution {
    pub count: u64,
    pub min_ns: Option<u64>,
    pub mean_ns: Option<u64>,
    pub max_ns: Option<u64>,
    /// Nearest-rank histogram upper bounds, not fabricated exact percentiles.
    pub p50_upper_ns: Option<u64>,
    pub p90_upper_ns: Option<u64>,
    pub p99_upper_ns: Option<u64>,
    pub p999_upper_ns: Option<u64>,
    pub bin_width_ns: u64,
    pub bins: Vec<u64>,
    pub overflow: u64,
    pub arithmetic_saturated: bool,
    pub raw: Vec<Sample>,
    pub raw_dropped: u64,
    pub raw_enabled: bool,
}

pub struct Recorder {
    bins: Vec<u64>,
    width: u64,
    count: u64,
    sum: u128,
    min: u64,
    max: u64,
    overflow: u64,
    saturated: bool,
    raw: Vec<Sample>,
    raw_limit: usize,
    raw_len: usize,
    raw_enabled: bool,
}
impl Recorder {
    /// All sample storage is allocated and touched before the measured phase.
    pub fn new(width: u64, bins: usize, raw_limit: Option<usize>) -> Result<Self, &'static str> {
        let raw_count = raw_limit.unwrap_or(0);
        if width == 0 || bins == 0 || bins > 1_000_000 || raw_count > 1_000_000 {
            return Err("invalid recorder bounds");
        }
        let mut histogram = vec![0; bins];
        for bucket in histogram.iter_mut().step_by(512) {
            *bucket = 1;
            std::hint::black_box(&mut *bucket);
            *bucket = 0;
        }
        if let Some(last) = histogram.last_mut() {
            *last = 1;
            std::hint::black_box(&mut *last);
            *last = 0;
        }
        let mut raw = vec![Sample::default(); raw_count];
        for sample in raw.iter_mut().step_by(128) {
            sample.sequence = 1;
            std::hint::black_box(&mut *sample);
            sample.sequence = 0;
        }
        if let Some(last) = raw.last_mut() {
            last.sequence = 1;
            std::hint::black_box(&mut *last);
            last.sequence = 0;
        }
        Ok(Self {
            bins: histogram,
            width,
            count: 0,
            sum: 0,
            min: u64::MAX,
            max: 0,
            overflow: 0,
            saturated: false,
            raw,
            raw_limit: raw_count,
            raw_len: 0,
            raw_enabled: raw_limit.is_some(),
        })
    }
    pub fn record(&mut self, target: u64, observed: u64) -> Result<(), &'static str> {
        let value = observed
            .checked_sub(target)
            .ok_or("clock moved backwards or worker woke early")?;
        let Some(count) = self.count.checked_add(1) else {
            self.saturated = true;
            return Err("sample count overflow");
        };
        self.count = count;
        self.min = self.min.min(value);
        self.max = self.max.max(value);
        if let Some(sum) = self.sum.checked_add(u128::from(value)) {
            self.sum = sum;
        } else {
            self.saturated = true;
        }
        if let Some(bucket) = usize::try_from(value / self.width)
            .ok()
            .and_then(|i| self.bins.get_mut(i))
        {
            *bucket += 1;
        } else {
            self.overflow += 1;
        }
        if self.raw_enabled && self.raw_len < self.raw_limit {
            self.raw[self.raw_len] = Sample {
                sequence: self.count - 1,
                target_ns: target,
                observed_ns: observed,
            };
            self.raw_len += 1;
        }
        Ok(())
    }
    pub fn finish(mut self) -> Distribution {
        let percentile = |per_mille: u64| {
            if self.count == 0 {
                return None;
            }
            let rank = (u128::from(self.count) * u128::from(per_mille)).div_ceil(1000) as u64;
            let mut cumulative = 0;
            for (i, &count) in self.bins.iter().enumerate() {
                cumulative += count;
                if cumulative >= rank {
                    return (i as u64 + 1)
                        .checked_mul(self.width)
                        .and_then(|n| n.checked_sub(1));
                }
            }
            None
        };
        let p = [
            percentile(500),
            percentile(900),
            percentile(990),
            percentile(999),
        ];
        self.raw.truncate(self.raw_len);
        Distribution {
            count: self.count,
            min_ns: (self.count > 0).then_some(self.min),
            mean_ns: if self.count > 0 && !self.saturated {
                Some((self.sum / u128::from(self.count)) as u64)
            } else {
                None
            },
            max_ns: (self.count > 0).then_some(self.max),
            p50_upper_ns: p[0],
            p90_upper_ns: p[1],
            p99_upper_ns: p[2],
            p999_upper_ns: p[3],
            bin_width_ns: self.width,
            bins: self.bins,
            overflow: self.overflow,
            arithmetic_saturated: self.saturated,
            raw: self.raw,
            raw_dropped: if self.raw_enabled {
                self.count - self.raw_len as u64
            } else {
                0
            },
            raw_enabled: self.raw_enabled,
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Series {
    pub name: String,
    pub worker: usize,
    pub clock_domain: String,
    pub scheduling: String,
    pub distribution: Distribution,
    pub skipped_periods: u64,
    pub overruns: u64,
    pub elapsed_ns: u64,
    pub work_units: u64,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct WorkerReport {
    pub job: crate::config::Job,
    pub start_ns: u64,
    pub end_ns: u64,
    pub memory_mode: String,
    pub series: Vec<Series>,
    pub fairness: Option<f64>,
    pub load_work_units: Vec<u64>,
}
/// Progress balance for identical work over a common window; not CPU utilization.
pub fn jain(values: &[u64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let sum = values.iter().map(|&x| x as f64).sum::<f64>();
    let squares = values.iter().map(|&x| (x as f64).powi(2)).sum::<f64>();
    (squares > 0.0).then(|| sum * sum / (values.len() as f64 * squares))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn quantile_bounds_cover_sorted_reference_samples() -> Result<(), &'static str> {
        let mut r = Recorder::new(17, 100, Some(0))?;
        let mut values = Vec::new();
        for n in 0..1001 {
            let value = n * 379 % 1500;
            values.push(value);
            r.record(10_000, 10_000 + value)?;
        }
        values.sort_unstable();
        let d = r.finish();
        for (p, actual) in [
            (500, d.p50_upper_ns),
            (900, d.p90_upper_ns),
            (990, d.p99_upper_ns),
            (999, d.p999_upper_ns),
        ] {
            let rank = (values.len() * p).div_ceil(1000);
            let exact = values[rank - 1];
            assert_eq!(actual, Some(exact / 17 * 17 + 16));
        }
        assert_eq!(d.raw_dropped, 1001);
        assert_eq!(d.count, d.bins.iter().sum::<u64>() + d.overflow);
        assert_eq!(d.mean_ns, Some(values.iter().sum::<u64>() / 1001));
        Ok(())
    }
    #[test]
    fn buckets_have_inclusive_upper_bounds_and_overflow_is_not_a_percentile()
    -> Result<(), &'static str> {
        let mut r = Recorder::new(10, 3, Some(2))?;
        for n in [0, 9, 10, 29, 30, 1000] {
            assert!(r.record(100, 100 + n).is_ok());
        }
        let d = r.finish();
        assert_eq!(d.bins, vec![2, 1, 1]);
        assert_eq!(d.overflow, 2);
        assert_eq!(d.p50_upper_ns, Some(19));
        assert_eq!(d.p99_upper_ns, None);
        assert_eq!(d.count, 6);
        assert_eq!(d.raw.len(), 2);
        assert_eq!(d.raw_dropped, 4);
        assert_eq!(d.max_ns, Some(1000));
        Ok(())
    }
    #[test]
    fn zero_samples_and_backwards_clock_are_explicit() -> Result<(), &'static str> {
        let mut r = Recorder::new(1, 10, None)?;
        assert!(r.record(11, 10).is_err());
        let d = r.finish();
        assert_eq!(d.count, 0);
        assert_eq!(d.mean_ns, None);
        assert_eq!(d.p50_upper_ns, None);
        assert_eq!(jain(&[]), None);
        assert_eq!(jain(&[0, 0]), None);
        assert_eq!(jain(&[4, 4]), Some(1.0));
        assert!(Recorder::new(0, 10, None).is_err());
        Ok(())
    }
}
