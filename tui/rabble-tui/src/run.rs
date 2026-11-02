//! The terminal loop. One channel carries both keyboard/mouse input (a thread blocked in
//! `crossterm::event::read`) and engine frames (the client's reader thread), so the loop is
//! `recv, drain everything queued, paint once`. A burst of input or a replay costs one paint,
//! never a backlog; engine I/O lives on its own threads and cannot stall a keystroke.
//! Each paint is wrapped in CSI ?2026 (synchronized output) and written as one buffer.

use std::{
    fs::File,
    io::{self, Write},
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, RecvTimeoutError},
        Arc,
    },
    time::{Duration, Instant},
};

use crossterm::{
    event::{self, DisableBracketedPaste, DisableFocusChange, DisableMouseCapture, EnableBracketedPaste, EnableFocusChange, EnableMouseCapture, Event},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{backend::CrosstermBackend, Terminal};

use crate::{
    app::{App, Config, Connector, Effect, Msg, Start},
    client::{self, Net},
    prefs::{self, Settings},
    theme::Theme,
    util,
};

pub const BSU: &[u8] = b"\x1b[?2026h";
pub const ESU: &[u8] = b"\x1b[?2026l";
/// Engine frames waiting for the loop; a reader that gets ahead blocks (the socket backs up).
pub const NET_QUEUE: usize = 1024;

pub struct Opts {
    pub socket: PathBuf,
    pub cwd: String,
    pub start: Start,
    pub model: Option<String>,
    pub latency_log: Option<PathBuf>,
}

/// The terminal's output buffer. One paint is one `flush`; if the process panics mid-frame the
/// half-built frame is DROPPED (a `BufWriter` would flush it to the main screen on unwind).
pub struct FrameBuf<W: Write>(Vec<u8>, W);

impl<W: Write> Write for FrameBuf<W> {
    fn write(&mut self, b: &[u8]) -> io::Result<usize> {
        self.0.extend_from_slice(b);
        Ok(b.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        self.1.write_all(&self.0)?;
        self.0.clear();
        self.1.flush()
    }
}

type Term = Terminal<CrosstermBackend<FrameBuf<io::Stdout>>>;

fn paint(term: &mut Term, app: &mut App) -> io::Result<()> {
    term.backend_mut().write_all(BSU)?;
    term.autoresize()?; // a resize that landed mid-frame is picked up here, before drawing
    term.draw(|f| app.draw(f))?;
    term.backend_mut().write_all(ESU)?;
    Write::flush(term.backend_mut())
}

fn apply_effects(app: &mut App, term: &mut Term) -> io::Result<()> {
    for e in app.effects.drain(..) {
        match e {
            Effect::Copy(t) => {
                term.backend_mut().write_all(util::osc52(&t).as_bytes())?;
                std::thread::spawn(move || util::copy_to_system(&t));
            }
        }
    }
    Ok(())
}

pub fn pct(v: &mut [f64], p: f64) -> f64 {
    if v.is_empty() {
        return 0.0;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    v[((v.len() - 1) as f64 * p) as usize]
}

/// Undo every terminal mode we set, in the order that cannot leave a half frame on screen:
/// end synchronized output first (a terminal holding a frame would swallow the rest).
pub fn restore() -> io::Result<()> {
    let mut out = io::stdout();
    restore_modes(&mut out)?;
    disable_raw_mode()
}

pub fn restore_modes(out: &mut impl Write) -> io::Result<()> {
    out.write_all(ESU)?;
    execute!(out, DisableFocusChange, DisableBracketedPaste, DisableMouseCapture, LeaveAlternateScreen)?;
    out.flush()
}

pub fn run(o: Opts) -> io::Result<()> {
    let th = Theme::from_env();
    let mut settings = Settings::load(&prefs::default_path());
    if o.model.is_some() {
        settings.model = o.model.clone();
    }
    let (tx, rx) = mpsc::channel::<Msg>();
    let (net_tx, net_rx) = mpsc::sync_channel::<Net>(NET_QUEUE);
    let wake_flag = Arc::new(AtomicBool::new(false));
    let wake: Arc<dyn Fn() + Send + Sync> = {
        let (flag, tx) = (wake_flag.clone(), tx.clone());
        Arc::new(move || {
            if !flag.swap(true, Ordering::AcqRel) {
                let _ = tx.send(Msg::Wake);
            }
        })
    };
    let name = format!("rabble-tui-{}", std::process::id());
    let connector: Connector = {
        let (sock, name, net_tx, wake) = (o.socket.clone(), name.clone(), net_tx.clone(), wake.clone());
        Arc::new(move || client::connect(&sock, &name, net_tx.clone(), wake.clone()))
    };
    let handle = connector().map_err(|e| {
        io::Error::new(e.kind(), format!("no engine at {}: {e}\nstart one with: python -m orchestra.serve --socket {}", o.socket.display(), o.socket.display()))
    })?;
    let mut app = App::new(Config { cwd: o.cwd, start: o.start, client: name }, th, settings, handle);
    app.post = Some(tx.clone());
    app.connector = Some(connector);

    let prev_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |i| {
        let _ = restore();
        prev_hook(i);
    }));
    enable_raw_mode()?;
    execute!(io::stdout(), EnterAlternateScreen, EnableMouseCapture, EnableBracketedPaste, EnableFocusChange)?;
    let mut term = Terminal::new(CrosstermBackend::new(FrameBuf(Vec::with_capacity(1 << 16), io::stdout())))?;

    // SIGTERM / SIGHUP (a closed terminal window, a kill) leave through the normal path so the
    // terminal modes are restored
    if let Ok(mut sigs) = signal_hook::iterator::Signals::new([signal_hook::consts::SIGTERM, signal_hook::consts::SIGHUP]) {
        let stx = tx.clone();
        std::thread::Builder::new().name("signals".into()).spawn(move || {
            if let Some(sig) = sigs.forever().next() {
                let _ = stx.send(Msg::Quit(format!("terminated (signal {sig})")));
            }
        })?;
    }
    let itx = tx.clone();
    std::thread::Builder::new().name("input".into()).spawn(move || {
        loop {
            match event::read() {
                Ok(ev) => {
                    if itx.send(Msg::Input(ev, Instant::now())).is_err() {
                        return;
                    }
                }
                Err(e) => {
                    let _ = itx.send(Msg::Quit(format!("terminal input failed: {e}")));
                    return;
                }
            }
        }
    })?;

    let mut lat_input: Vec<f64> = Vec::new();
    let mut lat_wheel: Vec<f64> = Vec::new();
    let result = (|| -> io::Result<()> {
        paint(&mut term, &mut app)?;
        loop {
            let mut wait = if app.needs_tick() { Duration::from_millis(50) } else { Duration::from_secs(1) };
            if let Some(due) = app.face_due() {
                wait = wait.min(due.saturating_duration_since(Instant::now()));
            }
            let mut first_input: Option<(Instant, bool)> = None;
            let note = |m: &Msg, first: &mut Option<(Instant, bool)>| {
                if let Msg::Input(e, at) = m {
                    let wheel = matches!(e, Event::Mouse(mm) if matches!(mm.kind, event::MouseEventKind::ScrollUp | event::MouseEventKind::ScrollDown));
                    if first.is_none() {
                        *first = Some((*at, wheel));
                    }
                }
            };
            match rx.recv_timeout(wait) {
                Ok(m) => {
                    note(&m, &mut first_input);
                    app.on_msg(m);
                    // all input and worker results first, so typing is never queued behind a replay
                    while let Ok(m) = rx.try_recv() {
                        note(&m, &mut first_input);
                        app.on_msg(m);
                    }
                }
                Err(RecvTimeoutError::Timeout) => app.dirty |= app.needs_tick(),
                Err(RecvTimeoutError::Disconnected) => return Ok(()),
            }
            // then the engine's frames, at most one queue's worth per paint
            wake_flag.store(false, Ordering::Release);
            for _ in 0..NET_QUEUE {
                match net_rx.try_recv() {
                    Ok(n) => app.on_net(n),
                    Err(_) => break,
                }
            }
            if app.quit {
                return Ok(());
            }
            // the face's frame: only on an iteration with no keyboard or wheel input, so it never sits in front of one
            if first_input.is_none() {
                app.tick_face(Instant::now());
            }
            if app.dirty {
                paint(&mut term, &mut app)?;
                apply_effects(&mut app, &mut term)?;
                if let Some((at, wheel)) = first_input {
                    let ms = at.elapsed().as_secs_f64() * 1e3;
                    if wheel { lat_wheel.push(ms) } else { lat_input.push(ms) }
                }
            }
        }
    })();
    let _ = restore();
    if let Some(why) = &app.quit_reason {
        eprintln!("rabble-tui: {why}");
    }
    if let Some(p) = &o.latency_log {
        let mut f = File::create(p)?;
        writeln!(f, "kind,n,p50_ms,p99_ms")?;
        for (k, v) in [("input", &mut lat_input), ("wheel", &mut lat_wheel)] {
            writeln!(f, "{k},{},{:.3},{:.3}", v.len(), pct(v, 0.5), pct(v, 0.99))?;
        }
    }
    result
}

/// Headless T1 / wheel measurement on real content: replay a recorded turn `repeat` times into an
/// App, then time key-to-paint (update + layout + draw + diff into a buffer) and wheel-to-paint.
/// This is the software cost; `scripts/bench`-style pty runs add the kernel and terminal.
pub fn bench(file: &std::path::Path, repeat: usize, w: u16, h: u16) -> io::Result<String> {
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseEvent, MouseEventKind};
    use rabble_proto::Frame;
    use ratatui::backend::TestBackend;

    let (handle, _rx) = client::Handle::detached();
    let cfg = Config { cwd: "/tmp".into(), start: Start::New { resume: None }, client: "bench".into() };
    let mut app = App::new(cfg, Theme::from_env(), Settings::default(), handle);
    let lines: Vec<String> = std::fs::read_to_string(file)?.lines().filter(|l| !l.trim().is_empty()).map(str::to_owned).collect();
    app.session = Some("b".into());
    for i in 0..repeat {
        for l in &lines {
            let mut f = Frame::parse(l).map_err(io::Error::other)?;
            f.session = Some("b".into());
            if let rabble_proto::Down::ToolCall { id, .. } | rabble_proto::Down::ToolResult { id, .. } = &mut f.msg {
                *id = format!("{id}-{i}");
            }
            if matches!(f.msg, rabble_proto::Down::PermissionAsk { .. } | rabble_proto::Down::PermissionResolved { .. }) {
                continue;
            }
            app.on_frame(f);
        }
    }
    let mut term = Terminal::new(TestBackend::new(w, h))?;
    term.draw(|f| app.draw(f))?;
    let total = app.t.total_lines();
    let key = |c: char| Event::Key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
    let mut keys = Vec::new();
    for i in 0..2000 {
        let t0 = Instant::now();
        app.on_input(key(if i % 7 == 6 { ' ' } else { (b'a' + (i % 26) as u8) as char }));
        term.draw(|f| app.draw(f))?;
        keys.push(t0.elapsed().as_secs_f64() * 1e3);
        if i % 50 == 49 {
            app.composer.set_text("");
        }
    }
    let wheel = |down: bool| Event::Mouse(MouseEvent { kind: if down { MouseEventKind::ScrollDown } else { MouseEventKind::ScrollUp }, column: 5, row: 5, modifiers: KeyModifiers::NONE });
    let mut wheels = Vec::new();
    for i in 0..4000 {
        let down = (i / 200) % 2 == 1;
        let t0 = Instant::now();
        app.on_input(wheel(down));
        term.draw(|f| app.draw(f))?;
        wheels.push(t0.elapsed().as_secs_f64() * 1e3);
    }
    Ok(format!(
        "{w}x{h} transcript {total} lines ({} entries)\nkey-to-paint  n={} p50 {:.3} ms p99 {:.3} ms max {:.3} ms\nwheel-to-paint n={} p50 {:.3} ms p99 {:.3} ms max {:.3} ms",
        app.t.len(), keys.len(), pct(&mut keys.clone(), 0.5), pct(&mut keys.clone(), 0.99), keys.iter().cloned().fold(0.0, f64::max),
        wheels.len(), pct(&mut wheels.clone(), 0.5), pct(&mut wheels.clone(), 0.99), wheels.iter().cloned().fold(0.0, f64::max)
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    #[derive(Clone, Default)]
    struct Sink(Arc<Mutex<Vec<u8>>>);
    impl Write for Sink {
        fn write(&mut self, b: &[u8]) -> io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(b);
            Ok(b.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn a_half_built_frame_is_discarded_on_drop_not_flushed() {
        let sink = Sink::default();
        {
            let mut f = FrameBuf(Vec::new(), sink.clone());
            f.write_all(b"frame one").unwrap();
            f.flush().unwrap();
            f.write_all(b"half of frame two").unwrap();
        } // unwinding from a panic drops it here
        assert_eq!(sink.0.lock().unwrap().as_slice(), b"frame one");
    }

    #[test]
    fn restore_ends_synchronized_output_before_leaving_the_alt_screen() {
        let mut out = Vec::new();
        restore_modes(&mut out).unwrap();
        let s = String::from_utf8(out).unwrap();
        assert!(s.starts_with("\x1b[?2026l"), "{s:?}");
        for seq in ["\x1b[?1004l", "\x1b[?2004l", "\x1b[?1000l", "\x1b[?1049l"] {
            assert!(s.contains(seq), "missing {seq:?} in {s:?}");
        }
        assert!(s.find("\x1b[?2026l").unwrap() < s.find("\x1b[?1049l").unwrap());
    }
}
