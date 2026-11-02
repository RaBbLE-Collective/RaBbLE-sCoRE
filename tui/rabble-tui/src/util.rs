//! Clipboard helpers: OSC 52 (reaches the local clipboard over SSH) plus wl-copy / xclip.

use std::{io::Write, process::{Command, Stdio}};

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn base64(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for c in data.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            if i <= c.len() {
                out.push(B64[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

pub fn osc52(text: &str) -> String {
    format!("\x1b]52;c;{}\x07", base64(text.as_bytes()))
}

pub fn copy_to_system(text: &str) {
    for cmd in [vec!["wl-copy"], vec!["xclip", "-selection", "clipboard"]] {
        if let Ok(mut c) = Command::new(cmd[0]).args(&cmd[1..]).stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null()).spawn() {
            if let Some(mut i) = c.stdin.take() {
                let _ = i.write_all(text.as_bytes());
            }
            let _ = c.wait();
            return;
        }
    }
}

/// Run a command and collect its stdout, killing it after `limit`.
pub fn output_within(cmd: &[&str], limit: std::time::Duration) -> Option<Vec<u8>> {
    use std::io::Read;
    let mut c = Command::new(cmd[0]).args(&cmd[1..]).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().ok()?;
    let mut out = c.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut v = Vec::new();
        let _ = out.read_to_end(&mut v);
        v
    });
    let start = std::time::Instant::now();
    loop {
        match c.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if start.elapsed() < limit => std::thread::sleep(std::time::Duration::from_millis(10)),
            _ => {
                let _ = c.kill();
                let _ = c.wait();
                break;
            }
        }
    }
    reader.join().ok()
}

/// Text on the desktop clipboard (Wayland, then X11); "" when there is none, it is not text, or
/// the tool hangs (2 s). Blocking: call it from a worker thread.
pub fn system_clipboard() -> String {
    let lim = std::time::Duration::from_secs(2);
    for cmd in [vec!["wl-paste", "--no-newline", "--type", "text"], vec!["xclip", "-selection", "clipboard", "-o", "-t", "UTF8_STRING"]] {
        if let Some(o) = output_within(&cmd, lim) {
            return String::from_utf8(o).unwrap_or_default();
        }
    }
    String::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_hung_command_is_killed_at_the_limit() {
        let t = std::time::Instant::now();
        let out = output_within(&["sleep", "30"], std::time::Duration::from_millis(150));
        assert!(t.elapsed() < std::time::Duration::from_secs(5) && out.is_some_and(|o| o.is_empty()));
        assert_eq!(output_within(&["echo", "hi"], std::time::Duration::from_secs(5)), Some(b"hi\n".to_vec()));
    }

    #[test]
    fn base64_matches_the_rfc_vectors() {
        for (i, o) in [("", ""), ("f", "Zg=="), ("fo", "Zm8="), ("foo", "Zm9v"), ("foob", "Zm9vYg=="), ("fooba", "Zm9vYmE="), ("foobar", "Zm9vYmFy")] {
            assert_eq!(base64(i.as_bytes()), o);
        }
        assert_eq!(osc52("hi"), "\x1b]52;c;aGk=\x07");
    }
}
