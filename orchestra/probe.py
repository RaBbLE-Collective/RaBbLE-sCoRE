"""Headless check of the engine: run one prompt, print events as JSON lines.

  python -m orchestra.probe "prompt" [--model haiku] [--cwd DIR] [--allow]

Permission asks are denied unless --allow.
Stdout is a fixture: redirect it to tests/fixtures/orchestra/<name>.jsonl to record one.
Runs in the RaBbLE-UI venv (~/.local/share/RaBbLE/rabble-ui/bin/python).
"""

import argparse
import asyncio
import sys

from . import events as ev


async def run(args) -> int:
    from .backend import ClaudeCodeBackend
    backend = ClaudeCodeBackend(args.cwd, model=args.model)
    await backend.start()
    await backend.send(args.prompt)
    status = 0
    stream = backend.events()
    wait = None  # after the Observer's turn ends, linger briefly for the context check
    try:
        while True:
            try:
                event = await asyncio.wait_for(anext(stream), wait)
            except (StopAsyncIteration, TimeoutError):
                break
            print(event.to_json(), flush=True)
            if isinstance(event, ev.PermissionAsk):
                backend.answer(event.id, ev.PermissionReply("allow" if args.allow else "deny", "probe"))
            elif isinstance(event, ev.Error):
                status = 1
            elif isinstance(event, ev.TurnDone) and event.origin == "observer":
                wait = 2.0
    finally:
        await backend.close()
    return status


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m orchestra.probe", description=__doc__.splitlines()[0])
    p.add_argument("prompt")
    p.add_argument("--model")
    p.add_argument("--cwd", default=".")
    p.add_argument("--allow", action="store_true", help="allow every permission ask")
    args = p.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
