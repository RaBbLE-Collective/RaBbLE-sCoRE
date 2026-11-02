//! The Observer's UI settings, shared with the Python UI: `$RABBLE_UI_PREFS` or
//! `~/.config/rabble/ui.json` (model, crew, mode, trust, park, park_continue). A bad or missing
//! file reads as defaults; a read-only home never stops the session.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

pub const MODES: [&str; 4] = ["default", "acceptEdits", "plan", "auto"];
pub const TRUST: [&str; 3] = ["strict", "standard", "trusted"];
pub const PARK_MODES: [&str; 3] = ["auto", "ask", "off"];
/// (label, value): aliases track the newest model of a tier; None is Claude Code's default.
pub const MODELS: [(&str, Option<&str>); 5] = [("default", None), ("opus", Some("opus")), ("sonnet", Some("sonnet")), ("haiku", Some("haiku")), ("fable", Some("claude-fable-5-1"))];

pub fn default_path() -> PathBuf {
    match std::env::var_os("RABBLE_UI_PREFS") {
        Some(p) => PathBuf::from(p),
        None => PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".config/rabble/ui.json"),
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Settings {
    pub model: Option<String>,
    pub crew: bool,
    pub mode: String,
    pub trust: Option<String>,
    pub park: String,
    pub park_continue: bool,
    pub path: Option<PathBuf>,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { model: None, crew: false, mode: "default".into(), trust: None, park: "auto".into(), park_continue: false, path: None }
    }
}

impl Settings {
    pub fn load(path: &Path) -> Settings {
        let mut s = Settings { path: Some(path.to_owned()), ..Default::default() };
        let Some(Value::Object(d)) = std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str(&t).ok()) else { return s };
        s.model = d.get("model").and_then(Value::as_str).filter(|m| !m.is_empty()).map(str::to_owned);
        s.crew = d.get("crew") == Some(&Value::Bool(true));
        s.trust = d.get("trust").and_then(Value::as_str).filter(|t| TRUST.contains(t)).map(str::to_owned);
        if let Some(m) = d.get("mode").and_then(Value::as_str).filter(|m| MODES.contains(m)) {
            s.mode = m.to_owned();
        }
        if let Some(p) = d.get("park").and_then(Value::as_str).filter(|p| PARK_MODES.contains(p)) {
            s.park = p.to_owned();
        }
        s.park_continue = d.get("park_continue") == Some(&Value::Bool(true));
        s
    }

    pub fn save(&self) {
        let Some(path) = &self.path else { return };
        let body = json!({"model": self.model, "crew": self.crew, "mode": self.mode, "trust": self.trust, "park": self.park, "park_continue": self.park_continue});
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        // temp + rename: a crash mid-write must not leave half a file for the Python UI to choke on
        let tmp = path.with_extension(format!("json.tmp{}", std::process::id()));
        if std::fs::write(&tmp, serde_json::to_string_pretty(&body).unwrap_or_default() + "\n").is_ok() && std::fs::rename(&tmp, path).is_err() {
            let _ = std::fs::remove_file(&tmp);
        }
    }
}

/// `/model` argument to a model value: a picker label, else whatever was typed (Claude Code
/// rejects what it does not know). `Some(None)` is "clear the override".
pub fn resolve_model(arg: &str) -> Option<String> {
    let arg = arg.trim();
    for (label, value) in MODELS {
        if arg.eq_ignore_ascii_case(label) {
            return value.map(str::to_owned);
        }
    }
    Some(arg.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings_roundtrip_and_bad_file() {
        let dir = std::env::temp_dir().join(format!("rt-prefs-{}", std::process::id()));
        let p = dir.join("sub/ui.json");
        let mut s = Settings::load(&p);
        assert_eq!(s, Settings { path: Some(p.clone()), ..Default::default() });
        s.model = Some("opus".into());
        s.crew = true;
        s.mode = "plan".into();
        s.trust = Some("strict".into());
        s.park = "ask".into();
        s.park_continue = true;
        s.save();
        assert_eq!(Settings::load(&p), s);
        let leftovers = std::fs::read_dir(p.parent().unwrap()).unwrap().filter(|e| e.as_ref().unwrap().file_name().to_string_lossy().contains("tmp")).count();
        assert_eq!(leftovers, 0, "the temp file is renamed away");
        std::fs::write(&p, "{not json").unwrap();
        assert_eq!(Settings::load(&p).mode, "default");
        std::fs::write(&p, r#"{"model":"","crew":"yes","mode":"bogus","trust":"nope","park":"x"}"#).unwrap();
        let b = Settings::load(&p);
        assert_eq!((b.model, b.crew, b.mode.as_str(), b.trust, b.park.as_str()), (None, false, "default", None, "auto"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn resolve_model_maps_labels_and_passes_ids_through() {
        assert_eq!(resolve_model("default"), None);
        assert_eq!(resolve_model("Opus"), Some("opus".into()));
        assert_eq!(resolve_model("fable"), Some("claude-fable-5-1".into()));
        assert_eq!(resolve_model(" claude-x-9 "), Some("claude-x-9".into()));
    }
}
