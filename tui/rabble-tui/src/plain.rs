//! Engine frames as plain text. R1's proof that the seam works: a replayed or live turn reads
//! as a transcript before any widgets exist. The Printer keeps just enough state to show streamed
//! text once (deltas as they arrive, the closing block only if nothing streamed).

use rabble_proto::{Down, Frame};

#[derive(Default)]
pub struct Printer {
    streamed: bool,
    mid_line: bool,
}

fn short(s: &str, n: usize) -> String {
    let s = s.replace('\n', " ");
    if s.chars().count() <= n { s } else { format!("{}…", s.chars().take(n).collect::<String>()) }
}

impl Printer {
    /// Text to write for this frame (may be empty). Ends a streamed line before anything else prints.
    pub fn render(&mut self, f: &Frame) -> String {
        let mut out = String::new();
        let mut line = |p: &mut Printer, s: String| {
            if p.mid_line {
                out.push('\n');
                p.mid_line = false;
            }
            out.push_str(&s);
            out.push('\n');
        };
        match &f.msg {
            Down::TextDelta { text, .. } => {
                self.streamed = true;
                self.mid_line = !text.ends_with('\n');
                return text.clone();
            }
            Down::TextBlock { organ, text } => {
                if !std::mem::take(&mut self.streamed) {
                    line(self, format!("{organ}: {text}"));
                } else if self.mid_line {
                    self.mid_line = false;
                    out.push('\n');
                }
            }
            Down::Hello { engine, user, .. } => line(self, format!("engine {engine}, you are {user}")),
            Down::Sessions { sessions } => {
                for s in sessions {
                    line(self, format!("session {} ({}, {} client(s)) {}", s.session, s.role, s.clients, s.cwd));
                }
                if sessions.is_empty() {
                    line(self, "no sessions".into());
                }
            }
            Down::SessionCreated {} => line(self, format!("created session {}", f.session.as_deref().unwrap_or("?"))),
            Down::Attached { role, .. } => line(self, format!("attached to {} as {role}", f.session.as_deref().unwrap_or("?"))),
            Down::Detached {} => line(self, "detached".into()),
            Down::SessionClosed {} => line(self, "session closed".into()),
            Down::SessionStarted { model, cwd, .. } => line(self, format!("session started: {model} in {cwd}")),
            Down::SessionResumed { session_id } => line(self, format!("session resumed: {}", short(session_id, 8))),
            Down::Capabilities { slash_commands, skills, mcp, problems, .. } => {
                line(self, format!("{} commands, {} skills, {} mcp server(s)", slash_commands.len(), skills.len(), mcp.len()));
                for p in problems {
                    line(self, format!("! {p}"));
                }
            }
            Down::UserSaid { sender, text, .. } => line(self, format!("{sender}> {text}")),
            Down::Thinking { .. } | Down::ThinkingText { .. } | Down::ModelUsed { .. } => {}
            Down::Narration { organ, text, .. } => {
                line(self, if organ == "rabble" { format!("  . {text}") } else { format!("  . {organ}: {text}") })
            }
            Down::ToolCall { organ, name, input, .. } => {
                let arg = input.values().find_map(|v| v.as_str()).unwrap_or("");
                line(self, format!("  [{organ}] {name} {}", short(arg, 70)));
            }
            Down::ToolResult { ok, summary, .. } => line(self, format!("    {} {}", if *ok { "ok" } else { "FAILED" }, short(summary, 70))),
            Down::PermissionAsk { id, tool, title, .. } => line(self, format!("? permission {id}: {tool} {}", title.as_deref().unwrap_or(""))),
            Down::PermissionResolved { id, decision, by } => line(self, format!("  permission {id} {decision:?} by {by}")),
            Down::OrganStarted { organ, brief, .. } => line(self, format!("  >> {organ}: {}", short(brief, 70))),
            Down::OrganProgress { .. } => {}
            Down::OrganDone { organ, status, result, .. } => line(self, format!("  << {organ} {status:?}: {}", short(result, 70))),
            Down::Usage { tokens, cost, context_pct } => {
                line(self, format!("usage {tokens} tokens{}{}", cost.map(|c| format!(", ${c:.4}")).unwrap_or_default(),
                    context_pct.map(|c| format!(", context {c:.0}%")).unwrap_or_default()))
            }
            Down::RateLimited { status, .. } => line(self, format!("rate limit: {status:?}")),
            Down::TurnDone { stop_reason, .. } => line(self, format!("-- turn done ({stop_reason})")),
            Down::EntropyChanged { band, trust, reason, .. } => line(self, format!("entropy {band}, trust {trust} {reason}")),
            Down::Error { text } => line(self, format!("error: {text}")),
            Down::Unknown => {}
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(lines: &[&str]) -> String {
        let mut p = Printer::default();
        lines.iter().map(|l| p.render(&Frame::parse(l).unwrap())).collect()
    }

    #[test]
    fn streamed_text_prints_once_and_the_block_closes_the_line() {
        let out = run(&[
            r#"{"type":"TextDelta","organ":"rabble","text":"Hel"}"#,
            r#"{"type":"TextDelta","organ":"rabble","text":"lo"}"#,
            r#"{"type":"TextBlock","organ":"rabble","text":"Hello"}"#,
            r#"{"type":"TurnDone","stop_reason":"end_turn","origin":"observer"}"#,
        ]);
        assert_eq!(out, "Hello\n-- turn done (end_turn)\n");
    }

    #[test]
    fn an_unstreamed_block_prints_with_its_organ() {
        assert_eq!(run(&[r#"{"type":"TextBlock","organ":"gobble","text":"found it"}"#]), "gobble: found it\n");
    }

    #[test]
    fn unknown_and_quiet_events_print_nothing() {
        assert_eq!(run(&[r#"{"type":"Mystery","text":"x"}"#, r#"{"type":"Thinking","organ":"rabble"}"#,
            r#"{"type":"ThinkingText","organ":"rabble","text":"hm"}"#]), "");
    }

    #[test]
    fn narration_prints_as_a_quiet_line() {
        assert_eq!(run(&[r#"{"type":"Narration","organ":"rabble","text":"Running the tests","id":"t1"}"#,
            r#"{"type":"Narration","organ":"gobble","text":"Reading `a.py`"}"#]), "  . Running the tests\n  . gobble: Reading `a.py`\n");
    }

    #[test]
    fn a_recorded_thinking_turn_prints_narration_but_not_thinking() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/orchestra/narration-turn.jsonl");
        let text = std::fs::read_to_string(path).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        let out = run(&lines);
        assert!(out.contains("  . Reading `orchestra/events.py`\n"), "{out}");
        assert!(!out.contains("The user wants"), "{out}");
    }

    #[test]
    fn a_recorded_turn_reads_as_a_transcript() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/orchestra/tool-turn.jsonl");
        let text = std::fs::read_to_string(path).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        let out = run(&lines);
        assert!(out.contains("session started"), "{out}");
        assert!(out.contains("[rabble] Bash ls RaBbLE-sCoRE/orchestra"), "{out}");
        assert!(out.contains("-- turn done"), "{out}");
    }
}
