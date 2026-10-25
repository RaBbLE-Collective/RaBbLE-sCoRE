"""`python -m ui`: RaBbLE's terminal face."""

import argparse
import os
import sys


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rabble-ui", description="RaBbLE's terminal face over Claude Code")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--model")
    p.add_argument("--continue", dest="cont", action="store_true")
    p.add_argument("--resume", metavar="ID")
    p.add_argument("--permission-mode", choices=["default", "acceptEdits", "plan"], default="default")
    p.add_argument("--replay", metavar="FILE")
    p.add_argument("--replay-delay", type=float, default=0.0)
    a = p.parse_args(argv)

    from ui.app import RabbleApp, SCORE_ROOT  # noqa: F401  (also puts crew/ on sys.path)
    from orchestra.backend import ClaudeCodeBackend, ReplayBackend

    cwd = os.path.abspath(os.path.expanduser(a.cwd))
    if a.replay:
        def factory(resume, cont):
            return ReplayBackend.from_file(a.replay, a.replay_delay)
    else:
        try:
            from roster import CREW_SUBDIR, build_lead_prompt, grimoire_root, load_crew
            grimoire = grimoire_root()
            lead = build_lead_prompt(load_crew(grimoire / CREW_SUBDIR), grimoire)
        except Exception as e:
            sys.stderr.write(f"rabble-ui: no lead persona ({e}); starting plain\n")
            lead = None

        def factory(resume, cont):
            return ClaudeCodeBackend(cwd, append=lead, model=a.model, permission_mode=a.permission_mode,
                                     resume=resume, continue_conversation=cont)

    RabbleApp(factory, cwd=cwd, model=a.model, mode=a.permission_mode,
              resume=a.resume, continue_=a.cont).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
