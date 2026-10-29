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
    p.add_argument("--permission-mode", choices=["default", "acceptEdits", "plan", "auto"])
    p.add_argument("--crew", dest="crew", action="store_true", default=None, help="organs on this launch")
    p.add_argument("--no-crew", dest="crew", action="store_false", help="organs off this launch")
    p.add_argument("--trust", choices=["strict", "standard", "trusted"], help="auto-approve level (default: earned from session entropy)")
    p.add_argument("--replay", metavar="FILE")
    p.add_argument("--replay-delay", type=float, default=0.0)
    a = p.parse_args(argv)

    from ui.app import RabbleApp, SCORE_ROOT  # noqa: F401  (also puts crew/ on sys.path)
    from orchestra.backend import ClaudeCodeBackend, ReplayBackend
    from ui.prefs import Settings

    cwd = os.path.abspath(os.path.expanduser(a.cwd))
    settings = Settings.load()
    # flags win for this launch and are saved only when the Observer changes a setting in the UI
    if a.model:
        settings.model = a.model
    if a.permission_mode:
        settings.mode = a.permission_mode
    if a.crew is not None:
        settings.crew = a.crew
    if a.trust:
        settings.trust = a.trust

    if a.replay:
        def factory(resume, cont):
            return ReplayBackend.from_file(a.replay, a.replay_delay)
    else:
        personas = grimoire = None
        try:
            from roster import CREW_SUBDIR, build_agents, build_ui_lead_prompt, grimoire_root, load_crew
            grimoire = grimoire_root()
            personas = load_crew(grimoire / CREW_SUBDIR)
        except Exception as e:
            sys.stderr.write(f"rabble-ui: no lead persona ({e}); starting plain\n")

        def factory(resume, cont):
            lead = agents = None
            if personas is not None:
                lead = build_ui_lead_prompt(personas, grimoire, settings.crew)
                agents = build_agents(personas) if settings.crew else None
            return ClaudeCodeBackend(cwd, append=lead, agents=agents, model=settings.model,
                                     permission_mode=settings.mode, resume=resume, continue_conversation=cont,
                                     trust_override=settings.trust, entropy=settings.carry)

    RabbleApp(factory, cwd=cwd, resume=a.resume, continue_=a.cont, settings=settings).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
