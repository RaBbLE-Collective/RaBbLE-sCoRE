# RaBbLE-sCoRE

The server behind RaBbLE: it answers chat for [joinrabble.world](https://joinrabble.world), carries voice for [RaBbLE-Pocket](https://github.com/RaBbLE-Collective/RaBbLE-Pocket), and runs the Crew of agents that turn a request into delegated work. It has three parts: a web API (FastAPI), a terminal app (`rabble`, Rust) and the original file-based dispatcher.

**Genesis release `0.1.0`** · Live at [score.joinrabble.world](https://score.joinrabble.world/health)

## Quickstart

Run the web API locally. You need Python 3 and a free [Groq](https://console.groq.com) API key.

```sh
cp server/secrets.example server/.secrets    # set GROQ_API_KEY and JWT_SECRET (16+ characters)
DEMO_MODE=true ./harness/local.sh            # makes a venv, writes server/.env, starts uvicorn on :8000
curl localhost:8000/health                   # reports the version
```

`DEMO_MODE=true` lets you chat without an account, from this machine only. Then:

```sh
curl -N localhost:8000/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"messages": [{"role": "user", "content": "hello"}]}'
```

## The terminal app

```sh
(cd tui && cargo build --release)   # the Rust shell
scripts/rabble                      # starts the engine if none is listening, then opens the shell
scripts/rabble --status
```

The engine runs in the RaBbLE-UI venv that RaBbLE-OS installs (`layerctl apply claude-code`). Without the Rust binary, `rabble` falls back to the Python UI. Options are in the header of `scripts/rabble`.

## The dispatcher

The original orchestrator writes a task file to `tasks/pending/`, a watcher routes it to an agent session, and the result lands in `tasks/done/`. It needs `tmux`, `inotify-tools` and Claude Code (`npm install -g @anthropic-ai/claude-code`):

```sh
./scripts/start-rabble.sh          # add --with-server for the web API too
tmux attach -t rabble-score
./scripts/stop-rabble.sh
```

## Web API

| Route | Purpose |
|---|---|
| `GET /health` | Health check and version |
| `POST /api/v1/chat` | Streaming chat |
| `POST /api/v1/workflows` | Start a workflow: `brainstorm`, `reflect`, `create`, `solve`, `learn`, `thrive` |
| `GET /api/v1/status` | Provider and quota info |
| `POST /api/v1/auth/register` | Create an account and API key |
| `/api/v1/sessions` | Create, list and message persistent chat sessions |
| `WS /api/v1/beam` | Voice beam for Pocket (speech in, speech out) |
| `WS /api/v1/node` | Node link for other machines |

**Hosting.** The server needs `JWT_SECRET` (16+ characters) and refuses to start without it. Set it, the provider keys and `RABBLE_ADMIN_KEY` in the host's environment, run `uvicorn main:app` from `server/`, and check `GET /health`.

**Pocket to Claude Code.** To have the Pocket's voice go to Claude Code on your laptop, set `BEAM_LLM_CHAIN=claude_code:sonnet`, `CC_COMMAND=claude` and `CC_WORKDIR=<the Collective root>` in `server/.env`, start `./harness/local.sh`, and point the Pocket at `ws://<laptop LAN address>:8000/api/v1/beam`. Details, including running Claude Code in a container: [Local Pocket Beam](https://github.com/RaBbLE-Collective/RaBbLE-Grimoire/blob/main/RaBbLE-sCoRE/RaBbLE-sCoRE-Local-Pocket-Beam.md).

## Layout

| Path | What |
|---|---|
| `server/` | The FastAPI web API |
| `orchestra/` | The engine: sessions over a Unix socket |
| `tui/` | The Rust terminal app |
| `ui/` | The earlier Python terminal UI |
| `crew/` | The Crew: RaBbLE and its organs as Claude Code personas |
| `agents/`, `tasks/`, `scripts/` | The dispatcher |
| `harness/` | Local server tooling |
| `tests/` | pytest suite |

## More

[Architecture](https://github.com/RaBbLE-Collective/RaBbLE-Grimoire/blob/main/RaBbLE-sCoRE/RaBbLE-sCoRE-Architecture.md) · [Crew](https://github.com/RaBbLE-Collective/RaBbLE-Grimoire/blob/main/RaBbLE-sCoRE/RaBbLE-sCoRE-Crew.md) · [Roadmap](https://github.com/RaBbLE-Collective/RaBbLE-Grimoire/blob/main/RaBbLE-sCoRE/RaBbLE-sCoRE-Roadmap.md) · [Versioning](https://github.com/RaBbLE-Collective/RaBbLE-Grimoire/blob/main/RaBbLE-Versioning.md) (the version lives in `VERSION`)

## Licence

Source-available under the RaBbLE Sovereign Accord: free for personal use, commercial use by agreement. See [LICENSE](LICENSE).
