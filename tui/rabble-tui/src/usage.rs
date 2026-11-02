//! Provider quota as the header shows it: Claude's 5-hour and weekly windows, read from the OS tracker's
//! cache (`~/.cache/rabble/llm-usage-latest.json`, same file and rules as `orchestra/usage.py`).
//! No daemon and no API call; with the tracker not running the fields are `None`.

use std::{path::PathBuf, time::{SystemTime, UNIX_EPOCH}};

pub const STALE_AFTER: f64 = 1200.0;
pub const WARN_AT: f64 = 70.0;
pub const HIGH_AT: f64 = 90.0;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Window {
    pub pct: f64,
    pub resets_at: Option<f64>, // epoch seconds
}

#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct Usage {
    pub five_hour: Option<Window>,
    pub week: Option<Window>,
    pub age: Option<f64>, // seconds since the tracker last wrote
}

impl Window {
    pub fn resets_in(&self, now: f64) -> Option<f64> {
        self.resets_at.map(|t| (t - now).max(0.0))
    }
    /// 0 ok, 1 warn, 2 high
    pub fn band(&self) -> u8 {
        if self.pct >= HIGH_AT { 2 } else if self.pct >= WARN_AT { 1 } else { 0 }
    }
}

impl Usage {
    pub fn stale(&self) -> bool {
        self.age.is_none_or(|a| a > STALE_AFTER)
    }
    pub fn is_empty(&self) -> bool {
        self.five_hour.is_none() && self.week.is_none()
    }
}

pub fn cache_path() -> PathBuf {
    match std::env::var_os("RABBLE_USAGE_FILE") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".cache/rabble/llm-usage-latest.json"),
    }
}

pub fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// `2026-11-02T21:49:59.859611+00:00` (or `Z`, or no offset = UTC) -> epoch seconds.
pub fn parse_iso(s: &str) -> Option<f64> {
    let (date, rest) = s.split_once('T')?;
    let mut d = date.split('-').map(|x| x.parse::<i64>().ok());
    let (y, m, day) = (d.next()??, d.next()??, d.next()??);
    let (time, off) = match rest.find(['+', '-', 'Z']) {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, ""),
    };
    let mut t = time.split(':');
    let (h, mi) = (t.next()?.parse::<f64>().ok()?, t.next()?.parse::<f64>().ok()?);
    let sec = t.next().map_or(Some(0.0), |x| x.parse::<f64>().ok())?;
    let off_s = match off.as_bytes().first() {
        Some(&sign @ (b'+' | b'-')) => {
            let (oh, om) = off[1..].split_once(':').unwrap_or((&off[1..], "0"));
            let v = oh.parse::<f64>().ok()? * 3600.0 + om.parse::<f64>().ok()? * 60.0;
            if sign == b'-' { -v } else { v }
        }
        _ => 0.0,
    };
    // days from civil (Hinnant)
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    Some(days as f64 * 86400.0 + h * 3600.0 + mi * 60.0 + sec - off_s)
}

fn window(raw: &serde_json::Value) -> (Option<Window>, Option<f64>) {
    let Some(pct) = raw.get("pct").and_then(|v| v.as_f64()) else { return (None, None) };
    let resets_at = raw.get("resets_at").and_then(|v| v.as_str()).and_then(parse_iso);
    (Some(Window { pct: pct.max(0.0), resets_at }), raw.get("ts").and_then(|v| v.as_f64()))
}

pub fn parse(text: &str, now: f64) -> Usage {
    let Ok(v) = serde_json::from_str::<serde_json::Value>(text) else { return Usage::default() };
    let (five_hour, t5) = v.get("5h").map_or((None, None), window);
    let (week, tw) = v.get("week").map_or((None, None), window);
    let stamp = [t5, tw].into_iter().flatten().fold(None, |a: Option<f64>, t| Some(a.map_or(t, |a| a.max(t))));
    Usage { five_hour, week, age: stamp.map(|t| now - t) }
}

pub fn read() -> Usage {
    std::fs::read_to_string(cache_path()).map(|t| parse(&t, now())).unwrap_or_default()
}

/// 4h21m, 38m, 2d3h: how long until a window resets.
pub fn fmt_remaining(seconds: Option<f64>) -> String {
    let Some(s) = seconds else { return "?".into() };
    let m = (s / 60.0) as i64;
    if m < 60 {
        return format!("{m}m");
    }
    let (h, m) = (m / 60, m % 60);
    if h < 24 {
        return format!("{h}h{m:02}m");
    }
    format!("{}d{}h", h / 24, h % 24)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_with_offset_matches_python() {
        assert_eq!(parse_iso("1970-01-02T00:00:00+00:00"), Some(86400.0));
        assert_eq!(parse_iso("1970-01-01T01:00:00+01:00"), Some(0.0));
        let t = parse_iso("2026-11-02T21:49:59.5Z").unwrap();
        assert!((t - 1793656199.5).abs() < 1e-6, "{t}");
    }

    #[test]
    fn cache_parses_and_goes_stale() {
        let j = r#"{"5h":{"ts":1000.0,"pct":60.0,"resets_at":"1970-01-01T01:00:00+00:00"},"week":{"ts":900.0,"pct":95}}"#;
        let u = parse(j, 1100.0);
        assert_eq!(u.five_hour.unwrap().pct, 60.0);
        assert_eq!(u.five_hour.unwrap().resets_in(0.0), Some(3600.0));
        assert_eq!(u.week.unwrap().band(), 2);
        assert!(!u.stale());
        assert!(parse(j, 5000.0).stale());
        assert!(parse("junk", 0.0).is_empty());
    }

    #[test]
    fn remaining_formats() {
        assert_eq!(fmt_remaining(Some(38.0 * 60.0)), "38m");
        assert_eq!(fmt_remaining(Some(4.0 * 3600.0 + 21.0 * 60.0)), "4h21m");
        assert_eq!(fmt_remaining(Some(51.0 * 3600.0)), "2d3h");
        assert_eq!(fmt_remaining(None), "?");
    }
}
