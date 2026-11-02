//! The engine connection: a reader thread and a writer thread behind a cheap `Handle`, so the
//! render thread never touches the socket. Frames arrive on a channel as `Net` values.
//!
//! * malformed lines (not JSON, or a body that does not decode) are counted in `Stats` and
//!   reported as `Net::Malformed`, never dropped silently;
//! * `Handle::request` stamps an `id` on a command and remembers its label; the reader tags the
//!   matching reply (`Net::Reply`) so the app can tell an answer to `/model` from an event;
//! * sequence numbers are the app's business (it tracks max(seq) of events only).

use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    sync::{
        atomic::{AtomicU64, Ordering},
        mpsc::{self, Receiver, Sender, SyncSender},
        Arc, Mutex,
    },
    thread,
};

use rabble_proto::{Decision, Down, Frame, Up, PROTOCOL};

pub fn default_socket() -> PathBuf {
    match std::env::var_os("XDG_RUNTIME_DIR") {
        Some(run) => PathBuf::from(run).join("rabble/engine.sock"),
        None => PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".local/state/rabble/engine.sock"),
    }
}

#[derive(Debug)]
pub enum Net {
    /// An event or an unsolicited message.
    Frame(Frame),
    /// The reply to a command sent with `request`; `label` is what the sender called it.
    Reply { label: String, frame: Frame },
    /// A line that did not decode: the reason and a clipped copy of the line.
    Malformed { why: String, line: String },
    /// The engine closed the connection (or the read failed).
    Closed(String),
}

#[derive(Default)]
pub struct Stats {
    pub frames: AtomicU64,
    pub malformed: AtomicU64,
}

/// Cheap to clone; sending never blocks (a writer thread owns the socket).
#[derive(Clone)]
pub struct Handle {
    tx: Sender<String>,
    next_id: Arc<AtomicU64>,
    pending: Arc<Mutex<HashMap<String, String>>>,
    pub stats: Arc<Stats>,
}

impl Handle {
    pub fn send(&self, up: &Up) {
        let _ = self.tx.send(up.to_line());
    }

    /// Send with a request id; the reply comes back as `Net::Reply { label, .. }`.
    pub fn request(&self, up: &Up, label: &str) -> String {
        let id = format!("r{}", self.next_id.fetch_add(1, Ordering::Relaxed));
        self.pending.lock().unwrap().insert(id.clone(), label.to_owned());
        let _ = self.tx.send(up.to_line_with_id(&id));
        id
    }

    pub fn pending(&self) -> usize {
        self.pending.lock().unwrap().len()
    }

    /// A handle wired to a channel instead of a socket (tests, benches).
    pub fn detached() -> (Handle, Receiver<String>) {
        let (tx, rx) = mpsc::channel();
        (Handle { tx, next_id: Arc::default(), pending: Arc::default(), stats: Arc::default() }, rx)
    }

    pub fn deny(&self, session: &str, ask_id: &str) {
        self.send(&Up::Answer { session: session.into(), ask_id: ask_id.into(), decision: Decision::Deny, reason: "rabble-tui probe denies".into() });
    }
}

/// A line from the engine is at most this long (one tool result can be a base64 screenshot).
pub const LINE_CAP: usize = 64 * 1024 * 1024;

pub enum Line {
    Eof,
    Ok(Vec<u8>),
    /// Longer than the cap: the whole line was consumed and discarded.
    TooLong,
}

/// Read one line without ever holding more than `cap` bytes of it.
pub fn read_capped(r: &mut impl BufRead, cap: usize) -> std::io::Result<Line> {
    use std::io::Read;
    let mut buf = Vec::new();
    let n = r.by_ref().take(cap as u64 + 1).read_until(b'\n', &mut buf)?;
    if n == 0 {
        return Ok(Line::Eof);
    }
    if buf.last() == Some(&b'\n') {
        buf.pop();
        return Ok(Line::Ok(buf));
    }
    if buf.len() <= cap {
        return Ok(Line::Ok(buf)); // final line without a newline
    }
    // too long: skip to the end of the line in bounded chunks
    loop {
        let mut sink = Vec::new();
        let n = r.by_ref().take(1 << 20).read_until(b'\n', &mut sink)?;
        if n == 0 || sink.last() == Some(&b'\n') {
            return Ok(Line::TooLong);
        }
    }
}

/// Connect, say hello, and start the threads. Frames go to the bounded `sink` (a reader that
/// outruns the app blocks, which backpressures the socket instead of growing memory); `wake` is
/// called after each push so the app's loop notices.
pub fn connect(path: &std::path::Path, name: &str, sink: SyncSender<Net>, wake: Arc<dyn Fn() + Send + Sync>) -> std::io::Result<Handle> {
    let stream = UnixStream::connect(path)?;
    let mut w = stream.try_clone()?;
    let r = BufReader::new(stream);
    let (tx, wrx) = mpsc::channel::<String>();
    let h = Handle { tx, next_id: Arc::default(), pending: Arc::default(), stats: Arc::default() };
    h.send(&Up::Hello { version: PROTOCOL, client: name.into() });
    thread::Builder::new().name("net-write".into()).spawn(move || {
        for line in wrx {
            if w.write_all(line.as_bytes()).is_err() {
                break;
            }
        }
    })?;
    let (pending, stats) = (h.pending.clone(), h.stats.clone());
    thread::Builder::new().name("net-read".into()).spawn(move || {
        let mut r = r;
        let push = |n: Net| {
            let ok = sink.send(n).is_ok();
            wake();
            ok
        };
        loop {
            let net = match read_capped(&mut r, LINE_CAP) {
                Ok(Line::Eof) => {
                    push(Net::Closed("engine closed the connection".into()));
                    return;
                }
                Err(e) => {
                    push(Net::Closed(e.to_string()));
                    return;
                }
                Ok(Line::TooLong) => {
                    stats.malformed.fetch_add(1, Ordering::Relaxed);
                    Net::Malformed { why: format!("line over {} MiB discarded", LINE_CAP >> 20), line: String::new() }
                }
                Ok(Line::Ok(bytes)) => match String::from_utf8(bytes) {
                    Ok(line) if line.trim().is_empty() => continue,
                    Ok(line) => classify(&line, &pending, &stats),
                    Err(e) => {
                        stats.malformed.fetch_add(1, Ordering::Relaxed);
                        Net::Malformed { why: "invalid UTF-8".into(), line: clip(&String::from_utf8_lossy(e.as_bytes())) }
                    }
                },
            };
            if !push(net) {
                return;
            }
        }
    })?;
    Ok(h)
}

fn clip(s: &str) -> String {
    s.trim_end().chars().take(160).collect()
}

/// One line to one `Net`. Pure apart from the counters and the pending map (unit tested).
pub fn classify(line: &str, pending: &Mutex<HashMap<String, String>>, stats: &Stats) -> Net {
    match Frame::parse(line) {
        Err(e) => {
            stats.malformed.fetch_add(1, Ordering::Relaxed);
            Net::Malformed { why: e.to_string(), line: clip(line) }
        }
        Ok(f) => {
            stats.frames.fetch_add(1, Ordering::Relaxed);
            if let Some(why) = &f.malformed {
                stats.malformed.fetch_add(1, Ordering::Relaxed);
                return Net::Malformed { why: why.clone(), line: clip(line) };
            }
            if let Some(id) = &f.id {
                if let Some(label) = pending.lock().unwrap().remove(id) {
                    return Net::Reply { label, frame: f };
                }
            }
            Net::Frame(f)
        }
    }
}

/// True when this frame ends the turn we are waiting on.
pub fn turn_over(f: &Frame) -> bool {
    matches!(f.msg, Down::TurnDone { .. } | Down::SessionClosed {})
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pend(id: &str, label: &str) -> Mutex<HashMap<String, String>> {
        Mutex::new(HashMap::from([(id.to_owned(), label.to_owned())]))
    }

    #[test]
    fn non_json_and_undecodable_lines_are_counted_not_dropped() {
        let (p, s) = (pend("x", "y"), Stats::default());
        assert!(matches!(classify("{not json", &p, &s), Net::Malformed { .. }));
        assert!(matches!(classify(r#"{"type":"Usage","tokens":"many"}"#, &p, &s), Net::Malformed { .. }));
        assert_eq!(s.malformed.load(Ordering::Relaxed), 2);
        assert!(matches!(classify(r#"{"type":"Usage","tokens":3}"#, &p, &s), Net::Frame(_)));
    }

    #[test]
    fn a_reply_is_matched_by_request_id_once() {
        let (p, s) = (pend("r1", "list"), Stats::default());
        let line = r#"{"type":"sessions","id":"r1","sessions":[]}"#;
        assert!(matches!(classify(line, &p, &s), Net::Reply { ref label, .. } if label == "list"));
        assert!(matches!(classify(line, &p, &s), Net::Frame(_)), "second copy is not a pending reply");
    }

    #[test]
    fn an_event_body_id_is_not_a_request_id() {
        let (p, s) = (pend("tu1", "list"), Stats::default());
        let line = r#"{"type":"ToolCall","organ":"r","id":"tu1","name":"Bash","input":{}}"#;
        assert!(matches!(classify(line, &p, &s), Net::Frame(_)));
    }

    #[test]
    fn handle_stamps_ids_and_remembers_labels() {
        let (h, rx) = Handle::detached();
        let id = h.request(&Up::ListSessions {}, "sessions");
        let line = rx.recv().unwrap();
        assert!(line.contains(&format!("\"id\":\"{id}\"")), "{line}");
        assert_eq!(h.pending(), 1);
    }

    #[test]
    fn reader_thread_delivers_frames_over_a_real_socket() {
        use std::os::unix::net::UnixListener;
        let dir = std::env::temp_dir().join(format!("rt-client-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let sock = dir.join("e.sock");
        let l = UnixListener::bind(&sock).unwrap();
        let srv = thread::spawn(move || {
            let (mut c, _) = l.accept().unwrap();
            let mut hello = String::new();
            BufReader::new(c.try_clone().unwrap()).read_line(&mut hello).unwrap();
            // non-JSON, invalid UTF-8 (must not be mistaken for a closed connection), then a good frame
            c.write_all(b"garbage\n\xff\xfe not utf8\n{\"type\":\"Error\",\"text\":\"boom\"}\n").unwrap();
            hello
        });
        let (tx, rx) = mpsc::sync_channel(8);
        let _h = connect(&sock, "t", tx, Arc::new(|| {})).unwrap();
        assert!(matches!(rx.recv().unwrap(), Net::Malformed { .. }));
        assert!(matches!(rx.recv().unwrap(), Net::Malformed { ref why, .. } if why.contains("UTF-8")));
        assert!(matches!(rx.recv().unwrap(), Net::Frame(f) if matches!(f.msg, Down::Error { .. })));
        assert!(srv.join().unwrap().contains("\"hello\""));
        assert!(matches!(rx.recv().unwrap(), Net::Closed(_)));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_line_over_the_cap_is_discarded_whole_and_the_next_line_survives() {
        let mut data = vec![b'x'; 5000];
        data.extend_from_slice(b"\nshort\nlast");
        let mut r = std::io::Cursor::new(data);
        assert!(matches!(read_capped(&mut r, 100).unwrap(), Line::TooLong));
        assert!(matches!(read_capped(&mut r, 100).unwrap(), Line::Ok(b) if b == b"short"));
        assert!(matches!(read_capped(&mut r, 100).unwrap(), Line::Ok(b) if b == b"last"));
        assert!(matches!(read_capped(&mut r, 100).unwrap(), Line::Eof));
        // exactly at the cap is fine
        let mut r = std::io::Cursor::new(b"abcd\n".to_vec());
        assert!(matches!(read_capped(&mut r, 4).unwrap(), Line::Ok(b) if b == b"abcd"));
    }

    #[test]
    fn the_frame_queue_is_bounded() {
        let (tx, _rx) = mpsc::sync_channel::<Net>(2);
        assert!(tx.try_send(Net::Closed("a".into())).is_ok() && tx.try_send(Net::Closed("b".into())).is_ok());
        assert!(tx.try_send(Net::Closed("c".into())).is_err(), "a full queue pushes back instead of growing");
    }
}
