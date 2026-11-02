//! Earlier Claude Code sessions for a project dir, read from `~/.claude/projects/<cwd>/*.jsonl`
//! (the SDK stores them there): id, a title from the first user message, and the mtime.

use std::{path::Path, time::UNIX_EPOCH};

use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub id: String,
    pub title: String,
    pub updated: u64,
}

fn title_of(v: &Value) -> Option<String> {
    if v.get("type")?.as_str()? != "user" {
        return None;
    }
    let c = v.get("message")?.get("content")?;
    let t = match c {
        Value::String(s) => s.clone(),
        Value::Array(a) => a.iter().find_map(|p| p.get("text").and_then(Value::as_str)).unwrap_or("").to_owned(),
        _ => return None,
    };
    let t = t.split_whitespace().collect::<Vec<_>>().join(" ");
    (!t.is_empty() && !t.starts_with('<') && !t.starts_with("Caveat")).then_some(t)
}

/// The first user message among the first 200 lines (lines too long to be a title are skipped unparsed).
pub fn head_title(r: impl std::io::BufRead) -> Option<String> {
    r.lines().take(200).map_while(Result::ok).filter(|l| l.len() < 1 << 20).filter_map(|l| serde_json::from_str::<Value>(&l).ok()).find_map(|v| title_of(&v))
}

pub fn project_dir(home: &Path, cwd: &Path) -> std::path::PathBuf {
    let enc: String = cwd.to_string_lossy().chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
    home.join(".claude/projects").join(enc)
}

pub fn recent(home: &Path, cwd: &Path, limit: usize) -> Vec<Row> {
    let Ok(rd) = std::fs::read_dir(project_dir(home, cwd)) else { return vec![] };
    let mut rows: Vec<Row> = rd
        .filter_map(|e| e.ok())
        .filter(|e| e.path().extension().is_some_and(|x| x == "jsonl"))
        .filter_map(|e| {
            let updated = e.metadata().ok()?.modified().ok()?.duration_since(UNIX_EPOCH).ok()?.as_secs();
            let id = e.path().file_stem()?.to_string_lossy().into_owned();
            Some(Row { id, title: String::new(), updated })
        })
        .collect();
    rows.sort_by(|a, b| b.updated.cmp(&a.updated));
    rows.truncate(limit);
    for r in &mut rows {
        // a session file can be tens of MB: read only the head, a line at a time
        let t = std::fs::File::open(project_dir(home, cwd).join(format!("{}.jsonl", r.id)))
            .ok()
            .and_then(|f| head_title(std::io::BufReader::new(std::io::Read::take(f, 4 << 20))))
            .unwrap_or_else(|| "(untitled)".into());
        r.title = if t.chars().count() > 70 { format!("{}...", t.chars().take(67).collect::<String>()) } else { t };
    }
    rows
}

pub fn ago(secs: u64, now: u64) -> String {
    let d = now.saturating_sub(secs);
    for (span, unit) in [(86400, "d"), (3600, "h"), (60, "m")] {
        if d >= span {
            return format!("{}{unit} ago", d / span);
        }
    }
    "just now".into()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lists_newest_first_with_titles() {
        let home = std::env::temp_dir().join(format!("rt-sess-{}", std::process::id()));
        let cwd = Path::new("/p/w");
        let dir = project_dir(&home, cwd);
        assert!(dir.ends_with(".claude/projects/-p-w"));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("old.jsonl"), "{\"type\":\"summary\"}\n{\"type\":\"user\",\"message\":{\"content\":\"<command>x</command>\"}}\n{\"type\":\"user\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"fix the  build\"}]}}\n").unwrap();
        std::thread::sleep(std::time::Duration::from_millis(1100));
        std::fs::write(dir.join("new.jsonl"), "{\"type\":\"user\",\"message\":{\"content\":\"newest work\"}}\n").unwrap();
        let rows = recent(&home, cwd, 10);
        assert_eq!(rows.iter().map(|r| (r.id.as_str(), r.title.as_str())).collect::<Vec<_>>(), [("new", "newest work"), ("old", "fix the build")]);
        assert!(recent(&home, Path::new("/none"), 10).is_empty());
        std::fs::remove_dir_all(&home).ok();
    }

    #[test]
    fn only_the_head_of_a_huge_file_is_read() {
        struct Endless(u64);
        impl std::io::Read for Endless {
            fn read(&mut self, b: &mut [u8]) -> std::io::Result<usize> {
                self.0 += b.len() as u64;
                assert!(self.0 < 8 << 20, "read far past the head");
                b.fill(b'\n');
                Ok(b.len())
            }
        }
        assert_eq!(head_title(std::io::BufReader::new(Endless(0))), None);
        let text = format!("{}{{\"type\":\"user\",\"message\":{{\"content\":\"late\"}}}}\n", "{\"type\":\"x\"}\n".repeat(200));
        assert_eq!(head_title(std::io::Cursor::new(text)), None, "a title after line 200 is not found");
    }

    #[test]
    fn ago_formats() {
        assert_eq!((ago(0, 30).as_str(), ago(0, 120).as_str(), ago(0, 7200).as_str(), ago(0, 200000).as_str()), ("just now", "2m ago", "2h ago", "2d ago"));
    }
}
