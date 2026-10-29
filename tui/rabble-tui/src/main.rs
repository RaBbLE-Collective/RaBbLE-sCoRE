//! R0 spike: 10k-line transcript, wheel scroll, synchronized output, SGR mouse.
//! Default feel is 1:1 like Vim / cat: an input event repaints on the same loop turn, no timer,
//! no easing; queued input is drained first so a burst costs one paint, never a backlog.
//! `rabble-tui [--log FILE]` writes per-paint timings on exit (input_to_paint_ms is the latency).
//! q / Esc quits. Feel A/B: 1 = instant (default), 2 = snappy glide, 3 = S292 glide;
//! +/- lines per notch; [ ] glide rate; , . minimum speed (glide modes only). Settings in the status bar.

use std::{
    fs::File,
    io::{self, Write},
    time::{Duration, Instant},
};

use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, MouseEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use rabble_tui::{
    glide::{Glide, GLIDE_FPS},
    transcript::{Transcript, View},
};
use ratatui::{backend::CrosstermBackend, layout::Rect, style::{Color, Style}, widgets::Paragraph, Terminal};

const BSU: &[u8] = b"\x1b[?2026h";
const ESU: &[u8] = b"\x1b[?2026l";

struct Row {
    t_ms: f64,
    draw_ms: f64,
    input_to_paint_ms: f64, // first unpainted input event to bytes flushed; 0 for timer frames
    pos: f64,
}

/// Returns true to quit.
fn handle(ev: Event, glide: &mut Glide, max: f64) -> bool {
    match ev {
        Event::Key(k) => match k.code {
            KeyCode::Char('q') | KeyCode::Esc => return true,
            KeyCode::Char(c @ ('1' | '2' | '3')) => glide.preset(c as u8 - b'0'),
            KeyCode::Char('-') => glide.lines = (glide.lines - 1.0).max(1.0),
            KeyCode::Char('+' | '=') => glide.lines = (glide.lines + 1.0).min(12.0),
            KeyCode::Char('[') => glide.rate = (glide.rate * 0.8).max(4.0),
            KeyCode::Char(']') => glide.rate = (glide.rate * 1.25).min(200.0),
            KeyCode::Char(',') => glide.min_speed = (glide.min_speed * 0.8).max(10.0),
            KeyCode::Char('.') => glide.min_speed = (glide.min_speed * 1.25).min(600.0),
            KeyCode::Home => glide.jump(0.0, max),
            KeyCode::End => glide.jump(max, max),
            KeyCode::PageDown | KeyCode::Char(' ') => glide.wheel(1.0, max),
            KeyCode::PageUp => glide.wheel(-1.0, max),
            _ => {}
        },
        Event::Mouse(m) => match m.kind {
            MouseEventKind::ScrollDown => glide.wheel(1.0, max),
            MouseEventKind::ScrollUp => glide.wheel(-1.0, max),
            _ => {}
        },
        _ => {}
    }
    false
}

fn main() -> io::Result<()> {
    let log = std::env::args().skip_while(|a| a != "--log").nth(1);
    let t = Transcript::synthetic(10_000);

    enable_raw_mode()?;
    execute!(io::stdout(), EnterAlternateScreen, EnableMouseCapture)?;
    let mut term = Terminal::new(CrosstermBackend::new(io::stdout()))?;

    let frame = Duration::from_secs_f64(1.0 / GLIDE_FPS as f64);
    let mut glide = Glide::default();
    glide.preset(1);
    let (mut last, mut next) = (Instant::now(), Instant::now());
    let mut dirty = true;
    let mut pending_input: Option<Instant> = None; // arrival of the oldest input not yet painted
    let mut rows: Vec<Row> = Vec::new();
    let t0 = Instant::now();

    let result = (|| -> io::Result<()> {
        loop {
            let h = term.size()?.height;
            let max = t.max_scroll(h.saturating_sub(1));
            let wait = if glide.active() { next.saturating_duration_since(Instant::now()) } else { Duration::from_millis(250) };
            if event::poll(wait)? {
                // drain everything queued so one paint covers the whole burst
                loop {
                    pending_input.get_or_insert_with(Instant::now);
                    if handle(event::read()?, &mut glide, max) {
                        return Ok(());
                    }
                    dirty = true;
                    if !event::poll(Duration::ZERO)? {
                        break;
                    }
                }
            }
            if glide.active() && Instant::now() >= next {
                let now = Instant::now();
                let dt = (now - last).as_secs_f64().min(0.05);
                last = now;
                dirty |= glide.tick(dt, max);
                next = (next + frame).max(now); // fixed cadence, no catch-up burst
            }
            if dirty {
                let start = Instant::now();
                io::stdout().write_all(BSU)?;
                term.draw(|f| {
                    let a = f.area();
                    f.render_widget(View { t: &t, pos: glide.pos }, Rect { height: a.height.saturating_sub(1), ..a });
                    let bar = format!(
                        " {} | {} lines/notch{} | 1 vim  2 snappy  3 glide  +/- [ ] , . ",
                        if glide.instant { "1:1 INSTANT" } else { "glide" },
                        glide.lines,
                        if glide.instant { String::new() } else { format!(" | rate {:.0} | min {:.0}/s", glide.rate, glide.min_speed) });
                    f.render_widget(Paragraph::new(bar).style(Style::new().fg(Color::Black).bg(Color::Rgb(0, 245, 255))),
                        Rect { y: a.y + a.height.saturating_sub(1), height: 1, ..a });
                })?;
                io::stdout().write_all(ESU)?;
                io::stdout().flush()?;
                dirty = false;
                if log.is_some() {
                    rows.push(Row {
                        t_ms: t0.elapsed().as_secs_f64() * 1e3,
                        draw_ms: start.elapsed().as_secs_f64() * 1e3,
                        input_to_paint_ms: pending_input.map_or(0.0, |i| i.elapsed().as_secs_f64() * 1e3),
                        pos: glide.pos,
                    });
                }
                pending_input = None;
            }
        }
    })();

    disable_raw_mode()?;
    execute!(io::stdout(), DisableMouseCapture, LeaveAlternateScreen)?;
    if let Some(path) = log {
        let mut f = File::create(path)?;
        writeln!(f, "t_ms,draw_ms,input_to_paint_ms,pos")?;
        for r in rows {
            writeln!(f, "{:.3},{:.3},{:.3},{:.2}", r.t_ms, r.draw_ms, r.input_to_paint_ms, r.pos)?;
        }
    }
    result
}
