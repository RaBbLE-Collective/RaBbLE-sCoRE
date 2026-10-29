"""Engine config: the Claude Code capabilities the harness passes through to the SDK.

Everything the Observer can set in Claude Code that is not a UI preference lives here:
MCP servers, plugins, tool allow/deny lists, extra directories, thinking and effort,
budget and turn caps, fallback model, env. The UI's own prefs stay in ui/prefs.py.

Files, later wins: ~/.config/rabble/engine.json ($RABBLE_ENGINE_CONFIG), then
<cwd>/.rabble/engine.json. Lists concatenate, dicts merge key by key, scalars replace.
A bad entry is dropped and named in `problems`; a bad file never stops a session.

  {
    "mcp_servers": {"notes": {"command": "uvx", "args": ["notes-mcp"]}, "web": {"type": "http", "url": "https://..."}},
    "plugins": ["~/plugins/mine"],
    "allowed_tools": ["Read", "Grep"], "disallowed_tools": ["WebFetch"],
    "add_dirs": ["~/RaBbLE-Collective"],
    "thinking": "adaptive" | "disabled" | 8000,      # int = token budget
    "effort": "low|medium|high|xhigh|max",
    "max_budget_usd": 5.0, "max_turns": 40, "fallback_model": "sonnet",
    "env": {"KEY": "value"}
  }
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EFFORTS = ("low", "medium", "high", "xhigh", "max")
_LISTS = ("allowed_tools", "disallowed_tools", "add_dirs", "plugins")
_KEYS = _LISTS + ("mcp_servers", "thinking", "effort", "max_budget_usd", "max_turns", "fallback_model", "env")


def default_path() -> Path:
    env = os.environ.get("RABBLE_ENGINE_CONFIG")
    return Path(env).expanduser() if env else Path.home() / ".config" / "rabble" / "engine.json"


def _strs(v: Any) -> list[str] | None:
    return list(v) if isinstance(v, list) and all(isinstance(x, str) and x for x in v) else None


@dataclass
class EngineConfig:
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    plugins: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    add_dirs: list[str] = field(default_factory=list)
    thinking: str | int | None = None
    effort: str | None = None
    max_budget_usd: float | None = None
    max_turns: int | None = None
    fallback_model: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, cwd: str | Path | None = None, user_path: Path | None = None) -> "EngineConfig":
        cfg = cls()
        paths = [user_path or default_path()]
        if cwd:
            paths.append(Path(cwd) / ".rabble" / "engine.json")
        for p in paths:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                cfg.problems.append(f"{p}: unreadable ({e})")
                continue
            if not isinstance(data, dict):
                cfg.problems.append(f"{p}: top level must be an object")
                continue
            cfg.sources.append(str(p))
            cfg._merge(data, p)
        return cfg

    def _merge(self, d: dict[str, Any], src: Path) -> None:
        def bad(key: str, why: str) -> None:
            self.problems.append(f"{src.name} {key}: {why}")

        for k in d:
            if k not in _KEYS:
                bad(k, "unknown key")
        for k in _LISTS:
            if k in d:
                v = _strs(d[k])
                if v is None:
                    bad(k, "expected a list of strings")
                elif k in ("add_dirs", "plugins"):
                    self.__dict__[k] += [str(Path(x).expanduser()) for x in v if str(Path(x).expanduser()) not in self.__dict__[k]]
                else:
                    self.__dict__[k] += [x for x in v if x not in self.__dict__[k]]
        servers = d.get("mcp_servers", {})
        if not isinstance(servers, dict):
            bad("mcp_servers", "expected an object of name: config")
        else:
            for name, conf in servers.items():
                if isinstance(conf, dict) and (conf.get("command") or conf.get("url")):
                    self.mcp_servers[name] = conf
                else:
                    bad(f"mcp_servers.{name}", "needs a command (stdio) or a url (http/sse)")
        if "thinking" in d:
            t = d["thinking"]
            if t in ("adaptive", "disabled") or (isinstance(t, int) and not isinstance(t, bool) and t > 0):
                self.thinking = t
            else:
                bad("thinking", "adaptive, disabled, or a token budget")
        if "effort" in d:
            if d["effort"] in EFFORTS:
                self.effort = d["effort"]
            else:
                bad("effort", f"one of {', '.join(EFFORTS)}")
        if "max_budget_usd" in d:
            v = d["max_budget_usd"]
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                self.max_budget_usd = float(v)
            else:
                bad("max_budget_usd", "a positive number")
        if "max_turns" in d:
            v = d["max_turns"]
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                self.max_turns = v
            else:
                bad("max_turns", "a positive integer")
        if "fallback_model" in d:
            v = d["fallback_model"]
            if isinstance(v, str) and v:
                self.fallback_model = v
            else:
                bad("fallback_model", "a model alias or id")
        if "env" in d:
            v = d["env"]
            if isinstance(v, dict) and all(isinstance(a, str) and isinstance(b, str) for a, b in v.items()):
                self.env.update(v)
            else:
                bad("env", "an object of string: string")

    def sdk_kwargs(self) -> dict[str, Any]:
        """Only what is set, so the SDK / Claude Code defaults stay in charge of the rest."""
        kw: dict[str, Any] = {}
        if self.mcp_servers:
            kw["mcp_servers"] = dict(self.mcp_servers)
        if self.plugins:
            kw["plugins"] = [{"type": "local", "path": p} for p in self.plugins]
        for k in ("allowed_tools", "disallowed_tools", "add_dirs", "effort", "max_budget_usd", "max_turns", "fallback_model"):
            if getattr(self, k):
                kw[k] = list(getattr(self, k)) if isinstance(getattr(self, k), list) else getattr(self, k)
        if self.env:
            kw["env"] = dict(self.env)
        if self.thinking is not None:
            kw["thinking"] = ({"type": self.thinking} if isinstance(self.thinking, str)
                              else {"type": "enabled", "budget_tokens": self.thinking})
        return kw

    def summary(self) -> str:
        bits = [f"mcp {len(self.mcp_servers)}" if self.mcp_servers else "", f"plugins {len(self.plugins)}" if self.plugins else "",
                f"effort {self.effort}" if self.effort else "", f"thinking {self.thinking}" if self.thinking is not None else "",
                f"budget ${self.max_budget_usd:g}" if self.max_budget_usd else "", f"turns {self.max_turns}" if self.max_turns else "",
                f"deny {len(self.disallowed_tools)}" if self.disallowed_tools else "",
                f"dirs +{len(self.add_dirs)}" if self.add_dirs else "",
                f"{len(self.problems)} config problem(s)" if self.problems else ""]
        return "engine " + (" ".join(b for b in bits if b) or "defaults")
