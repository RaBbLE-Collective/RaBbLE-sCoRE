//! rabble-tui: the RaBbLE shell. `rabble-tui` opens a session on a running engine
//! (`python -m orchestra.serve`); the other subcommands are the seam probes and benches.

use std::{io, path::PathBuf, sync::mpsc};

use rabble_proto::{Down, Frame, Up};
use rabble_tui::{
    app::Start,
    client::{self, Net},
    plain::Printer,
    run::{self, Opts},
};

const USAGE: &str = "rabble-tui [--socket PATH] [--attach ID] [--resume SDK_ID] [--cwd DIR] [--model M] [--latency-log FILE]
                                             the shell: a new session (or attach / resume) on a running engine
rabble-tui replay FILE                       print a recorded turn file as plain text
rabble-tui connect [--socket PATH] [--attach ID | --new [--cwd DIR]] [--say TEXT]
                                             talk to the engine, print frames as text
rabble-tui bench FILE [--repeat N] [--size WxH]
                                             headless key-to-paint and wheel-to-paint on real content
rabble-tui spike [--log FILE]                the R0 scroll spike (q quits)";

fn main() {
    if let Err(e) = real_main() {
        eprintln!("rabble-tui: {e}");
        std::process::exit(1);
    }
}

fn real_main() -> io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let flag = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let socket = || flag("--socket").map(PathBuf::from).unwrap_or_else(client::default_socket);
    match args.first().map(String::as_str) {
        Some("-h" | "--help") => {
            println!("{USAGE}");
            Ok(())
        }
        Some("spike") => rabble_tui::spike::run(flag("--log")),
        Some("replay") => {
            let file = args.get(1).ok_or_else(|| io::Error::other(USAGE))?;
            let mut p = Printer::default();
            for line in std::fs::read_to_string(file)?.lines().filter(|l| !l.trim().is_empty()) {
                let f = Frame::parse(line).map_err(|e| io::Error::other(format!("{file}: {e}")))?;
                print!("{}", p.render(&f));
            }
            Ok(())
        }
        Some("bench") => {
            let file = args.get(1).ok_or_else(|| io::Error::other(USAGE))?;
            let repeat = flag("--repeat").and_then(|v| v.parse().ok()).unwrap_or(150);
            let (w, h) = flag("--size").and_then(|v| v.split_once('x').and_then(|(a, b)| Some((a.parse().ok()?, b.parse().ok()?)))).unwrap_or((200, 55));
            println!("{}", run::bench(std::path::Path::new(file), repeat, w, h)?);
            Ok(())
        }
        Some("connect") => connect(&args, socket()),
        _ => match run::run(Opts {
            socket: socket(),
            cwd: flag("--cwd").unwrap_or_else(|| std::env::current_dir().map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|_| ".".into())),
            start: match (flag("--attach"), flag("--resume")) {
                (Some(id), _) => Start::Attach(id),
                (None, resume) => Start::New { resume },
            },
            model: flag("--model"),
            latency_log: flag("--latency-log").map(PathBuf::from),
        })? {
            Some(session) => reexec(&args, &session),
            None => Ok(()),
        },
    }
}

/// `/reload`: replace this process with the binary on disk (a fresh `cargo build` lands there), reattached to the
/// same session. The engine never stopped, so the transcript replays from its ring. The terminal is already restored.
fn reexec(args: &[String], session: &str) -> io::Result<()> {
    use std::os::unix::process::CommandExt;
    // a rebuilt binary replaces the file this process was started from, so Linux reports ".../rabble-tui (deleted)"
    let exe = std::env::current_exe()?;
    let exe = PathBuf::from(exe.to_string_lossy().trim_end_matches(" (deleted)"));
    let err = std::process::Command::new(&exe).args(run::reload_args(args, session)).exec();
    Err(io::Error::new(err.kind(), format!("reload: cannot start {}: {err}", exe.display())))
}

/// The R1 probe, now on the threaded client: print a turn as text.
fn connect(args: &[String], path: PathBuf) -> io::Result<()> {
    use std::io::Write;
    let flag = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let (tx, rx) = mpsc::sync_channel::<Net>(run::NET_QUEUE);
    let c = client::connect(&path, "rabble-tui-probe", tx, std::sync::Arc::new(|| {}))?;
    let mut p = Printer::default();
    let mut session = flag("--attach");
    if session.is_none() && args.iter().any(|a| a == "--new") {
        c.send(&Up::CreateSession { cwd: flag("--cwd"), resume: None, continue_last: None, model: flag("--model"), mode: None, crew: None, trust: None, provider: None });
    } else if let Some(s) = &session {
        c.send(&Up::Attach { session: s.clone(), since_seq: None });
    } else {
        c.send(&Up::ListSessions {});
    }
    let say = flag("--say");
    let mut said = false;
    for n in rx {
        let f = match n {
            Net::Frame(f) | Net::Reply { frame: f, .. } => f,
            Net::Malformed { why, line } => {
                eprintln!("malformed line ({why}): {line}");
                continue;
            }
            Net::Closed(_) => break,
        };
        print!("{}", p.render(&f));
        io::stdout().flush()?;
        if let (Some(sid), Down::SessionCreated {} | Down::Attached { .. }) = (&f.session, &f.msg) {
            session = Some(sid.clone());
        }
        if let (Some(text), Some(sid), false, Down::SessionStarted { .. } | Down::SessionResumed { .. } | Down::Attached { .. }) = (&say, &session, said, &f.msg) {
            said = true;
            c.send(&Up::Send { session: sid.clone(), text: text.clone() });
        }
        if let (Down::PermissionAsk { id, .. }, Some(sid)) = (&f.msg, &session) {
            c.deny(sid, id);
        }
        if (said && client::turn_over(&f)) || (say.is_none() && session.is_none() && matches!(f.msg, Down::Sessions { .. })) {
            break;
        }
    }
    Ok(())
}
