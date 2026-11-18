//! Parking: hand off to a fresh session before the context fills. Ports `orchestra/park.py`'s
//! prompts and handoff check (the model writes the handoff; the shell restarts the session).
//! Dropped from the Python version: the context-growth ETA (`Bloat`), see PARITY.md.

use std::{path::{Path, PathBuf}, time::{SystemTime, UNIX_EPOCH}};

pub const MIN_BYTES: u64 = 300;

pub fn park_at() -> f64 {
    std::env::var("RABBLE_PARK_AT").ok().and_then(|v| v.parse::<f64>().ok()).map_or(80.0, |v| v.clamp(1.0, 99.0))
}
pub fn warn_at() -> f64 {
    (park_at() - 10.0).max(1.0)
}

pub fn handoff_dir(cwd: &Path) -> PathBuf {
    let here = cwd.canonicalize().unwrap_or_else(|_| cwd.to_owned());
    for d in here.ancestors() {
        let g = d.join("RaBbLE-Grimoire/log/handoffs");
        if g.is_dir() {
            return g;
        }
        if d.join("log/handoffs").is_dir() && d.join("log/SESSION-LOG.md").exists() {
            return d.join("log/handoffs");
        }
    }
    here.join(".rabble/handoffs")
}

/// UTC `YYYYMMDD-HHMM` (days-from-civil inverse; no time crate offline).
pub fn stamp(secs: u64) -> String {
    let (days, rem) = ((secs / 86400) as i64, secs % 86400);
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}{m:02}{d:02}-{:02}{:02}", rem / 3600, rem % 3600 / 60)
}

pub fn handoff_path(cwd: &Path, session: Option<&str>) -> PathBuf {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs());
    let id: String = session.unwrap_or("session").chars().take(8).collect();
    handoff_dir(cwd).join(format!("HANDOFF-parked-{}-{id}.md", stamp(now)))
}

pub fn park_prompt(path: &Path, pct: f64) -> String {
    format!(
        "PARK THIS SESSION. Context is {pct:.0}% full. A fresh session will continue the work and this one \
will not be resumed, so the handoff must stand alone. Do these in order, tersely:\n\n\
1. Write the handoff to exactly this path: {}\n\
   Sections: the goal in one paragraph; state now (what works, what is half-done); what this \
session did (files, commits); what is next, ordered and concrete with the exact commands; \
open decisions waiting on Mark; things learned that the repo does not record; the files to read \
first. Under ~120 lines. The next session remembers nothing of this one.\n\
2. Add this session's entry to the session log, following the repo's own instructions (do not \
hand-write the LATEST box). If there is no session log here, skip this step.\n\
3. Commit only if the work is at a good commit point (one coherent change, checks pass). \
Otherwise leave it uncommitted and list the uncommitted files, and why, in the handoff. \
Never commit unrelated changes. Never push.\n\
4. Reply with one line: the handoff path. Do not start any new work.",
        path.display()
    )
}

pub fn fresh_prompt(path: &Path, keep_going: bool) -> String {
    let base = format!("This is a fresh session parked from an earlier one that ran out of room. Read {} in full, then the files it says to read first. ", path.display());
    if keep_going {
        base + "Then continue with its 'next' list from the first item. Stop and ask at anything that needs my decision."
    } else {
        base + "Then say in three lines where things stand and what you would do next, and wait for me."
    }
}

/// None when the handoff is there and real; otherwise why not.
pub fn check_handoff(path: &Path, since: SystemTime) -> Option<String> {
    let Ok(md) = std::fs::metadata(path) else { return Some(format!("no file at {}", path.display())) };
    let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    if md.modified().ok().is_some_and(|m| m + std::time::Duration::from_secs(1) < since) {
        return Some(format!("{name} was not written this turn"));
    }
    if md.len() < MIN_BYTES {
        return Some(format!("{name} is only {} bytes: a stub, not a cold start", md.len()));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stamp_is_a_utc_date() {
        assert_eq!(stamp(0), "19700101-0000");
        assert_eq!(stamp(1_761_696_000 + 3600 * 5 + 60 * 7), "20251029-0507");
    }

    #[test]
    fn a_stub_or_missing_handoff_is_refused() {
        let dir = std::env::temp_dir().join(format!("rt-park-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("h.md");
        assert!(check_handoff(&f, SystemTime::now()).unwrap().contains("no file"));
        std::fs::write(&f, "tiny").unwrap();
        assert!(check_handoff(&f, UNIX_EPOCH).unwrap().contains("stub"));
        std::fs::write(&f, "x".repeat(400)).unwrap();
        assert_eq!(check_handoff(&f, UNIX_EPOCH), None);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn prompts_carry_the_path() {
        let p = Path::new("/h/x.md");
        assert!(park_prompt(p, 81.0).contains("/h/x.md") && park_prompt(p, 81.0).contains("81%"));
        assert!(fresh_prompt(p, true).contains("continue") && fresh_prompt(p, false).contains("wait for me"));
    }
}
