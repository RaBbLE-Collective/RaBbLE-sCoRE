//! Talk to a running engine (`python -m orchestra.serve`) over its Unix socket. Blocking and
//! thread-free on purpose: the R1 proof prints a turn; the real shell will read on its own thread.

use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
};

use rabble_proto::{Decision, Down, Frame, Up, PROTOCOL};

pub fn default_socket() -> PathBuf {
    match std::env::var_os("XDG_RUNTIME_DIR") {
        Some(run) => PathBuf::from(run).join("rabble/engine.sock"),
        None => PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".local/state/rabble/engine.sock"),
    }
}

pub struct Client {
    w: UnixStream,
    r: BufReader<UnixStream>,
}

impl Client {
    pub fn connect(path: &std::path::Path, name: &str) -> std::io::Result<Client> {
        let w = UnixStream::connect(path)?;
        let r = BufReader::new(w.try_clone()?);
        let mut c = Client { w, r };
        c.send(&Up::Hello { version: PROTOCOL, client: name.into() })?;
        Ok(c)
    }

    pub fn send(&mut self, up: &Up) -> std::io::Result<()> {
        self.w.write_all(up.to_line().as_bytes())
    }

    /// Next frame; `Ok(None)` when the engine closed the connection. Unparseable lines are skipped.
    pub fn next(&mut self) -> std::io::Result<Option<Frame>> {
        loop {
            let mut line = String::new();
            if self.r.read_line(&mut line)? == 0 {
                return Ok(None);
            }
            if let Ok(f) = Frame::parse(&line) {
                return Ok(Some(f));
            }
        }
    }

    pub fn deny(&mut self, session: &str, ask_id: &str) -> std::io::Result<()> {
        self.send(&Up::Answer { session: session.into(), ask_id: ask_id.into(), decision: Decision::Deny, reason: "rabble-tui probe denies".into() })
    }
}

/// True when this frame ends the turn we are waiting on.
pub fn turn_over(f: &Frame) -> bool {
    matches!(f.msg, Down::TurnDone { .. } | Down::SessionClosed {})
}
