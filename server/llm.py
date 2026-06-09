import httpx, os, json, logging, asyncio
from typing import AsyncGenerator

logger = logging.getLogger("rabble.llm")

# ── Provider config ───────────────────────────────────────────────────────────

BUILTIN_PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "api_key_env": "GROQ_API_KEY",
        "extra_headers": {},
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "api_key_env": "OPENROUTER_API_KEY",
        "extra_headers": {
            "HTTP-Referer": os.getenv("FRONTEND_URL", ""),
            "X-Title": "RaBbLE",
        },
    },
    # Claude Code subprocess — calls `claude --print` using CC's existing auth
    "claude_code": {
        "type": "subprocess",
        "command": os.getenv("CC_COMMAND", "claude"),
        "api_key_env": "",
        "api_key_required": False,
        "extra_headers": {},
    },
    # Codex CLI subprocess — same pattern as CC
    "codex": {
        "type": "subprocess",
        "command": os.getenv("CODEX_COMMAND", "codex"),
        "api_key_env": "",
        "api_key_required": False,
        "extra_headers": {},
    },
    # Local inference: Ollama (11434), llama.cpp server, vllm — OpenAI-compatible
    "local_llm": {
        "url": os.getenv("LOCAL_LLM_URL", "http://localhost:11434") + "/v1/chat/completions",
        "api_key_env": "LOCAL_LLM_API_KEY",
        "extra_headers": {},
        "api_key_required": False,
    },
}

DEFAULT_MODEL_CHAINS = {
    "fast": [
        {"provider": "claude_code", "model": "claude-haiku-4-5-20251001"},
        {"provider": "local_llm",   "model": os.getenv("LOCAL_LLM_MODEL", "llama3.2")},
        {"provider": "groq",        "model": "llama-3.1-8b-instant"},
        {"provider": "openrouter",  "model": "google/gemma-4-26b-a4b-it:free"},
    ],
    "medium": [
        {"provider": "openrouter", "model": "google/gemma-4-26b-a4b-it:free"},
        {"provider": "groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "groq", "model": "llama-3.1-8b-instant"},
    ],
    "strong": [
        {"provider": "openrouter", "model": "anthropic/claude-sonnet-4-6"},
        {"provider": "groq", "model": "llama-3.3-70b-versatile"},
    ],
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _provider(provider_name: str) -> dict:
    return _provider_registry()[provider_name]

def _provider_registry() -> dict:
    configured = os.getenv("LLM_PROVIDERS_JSON", "").strip()
    if not configured:
        return BUILTIN_PROVIDERS
    try:
        overrides = json.loads(configured)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid LLM_PROVIDERS_JSON: {exc}") from exc
    if not isinstance(overrides, dict):
        raise RuntimeError("LLM_PROVIDERS_JSON must be a JSON object")
    providers = {**BUILTIN_PROVIDERS}
    for provider_name, config in overrides.items():
        if not isinstance(config, dict):
            raise RuntimeError(f"Provider '{provider_name}' config must be a JSON object")
        url = str(config.get("url", "")).strip()
        api_key_env = str(config.get("api_key_env", "")).strip()
        extra_headers = config.get("extra_headers", {})
        if not url or not api_key_env:
            raise RuntimeError(
                f"Provider '{provider_name}' must define both 'url' and 'api_key_env' in LLM_PROVIDERS_JSON"
            )
        if not isinstance(extra_headers, dict):
            raise RuntimeError(f"Provider '{provider_name}' extra_headers must be a JSON object")
        providers[provider_name] = {
            "url": url,
            "api_key_env": api_key_env,
            "extra_headers": {str(k): str(v) for k, v in extra_headers.items()},
            "api_key_required": bool(config.get("api_key_required", True)),
        }
    return providers

def _provider_api_key(provider_name: str) -> str | None:
    p = _provider(provider_name)
    if not p.get("api_key_required", True):
        # Key-optional providers (CC, local inference): return the key if set,
        # or empty string so _available_candidates doesn't skip this entry.
        return os.getenv(p["api_key_env"]) or ""
    return os.getenv(p["api_key_env"])

def _env_chain_name(tier: str) -> str:
    return f"LLM_{tier.upper()}_CHAIN"

def _parse_model_chain(value: str) -> list[dict]:
    chain = []
    for raw_entry in value.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(f"Invalid model chain entry '{entry}'. Expected provider:model")
        provider_name, model_name = entry.split(":", 1)
        provider_name = provider_name.strip()
        model_name = model_name.strip()
        if provider_name not in _provider_registry():
            raise ValueError(f"Unknown provider '{provider_name}' in model chain entry '{entry}'")
        if not model_name:
            raise ValueError(f"Missing model name in model chain entry '{entry}'")
        chain.append({"provider": provider_name, "model": model_name})
    if not chain:
        raise ValueError("Model chain cannot be empty")
    return chain

def _model_chain(tier: str) -> list[dict]:
    configured = os.getenv(_env_chain_name(tier), "").strip()
    if configured:
        return _parse_model_chain(configured)
    return DEFAULT_MODEL_CHAINS[tier]

def _headers(provider_name: str) -> dict:
    p = _provider(provider_name)
    key = _provider_api_key(provider_name)
    headers = {**p["extra_headers"]}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers

def _available_candidates(tier: str) -> list[dict]:
    candidates = []
    for candidate in _model_chain(tier):
        provider_name = candidate["provider"]
        p = _provider(provider_name)
        key_required = p.get("api_key_required", True)
        has_key = bool(os.getenv(p["api_key_env"]))
        if has_key or not key_required:
            candidates.append(candidate)
        else:
            logger.warning(
                "LLM candidate skipped tier=%s provider=%s model=%s reason=missing_api_key",
                tier,
                provider_name,
                candidate["model"],
            )
    if not candidates:
        raise RuntimeError(f"No configured LLM provider has an API key for tier '{tier}'")
    return candidates

def _request_metadata(tier: str, candidate: dict) -> dict:
    provider_name = candidate["provider"]
    provider = _provider(provider_name)
    return {
        "tier": tier,
        "provider": provider_name,
        "model": candidate["model"],
        "url": provider["url"],
    }

def _log_request_start(operation: str, tier: str, candidate: dict, messages: list[dict], stream: bool, attempt: int, total: int) -> dict:
    meta = _request_metadata(tier, candidate)
    logger.warning(
        "LLM request start operation=%s provider=%s tier=%s model=%s stream=%s url=%s message_count=%s attempt=%s/%s",
        operation,
        meta["provider"],
        meta["tier"],
        meta["model"],
        stream,
        meta["url"],
        len(messages),
        attempt,
        total,
    )
    return meta

def _log_response_status(operation: str, meta: dict, status_code: int):
    logger.warning(
        "LLM response operation=%s provider=%s tier=%s model=%s status=%s",
        operation,
        meta["provider"],
        meta["tier"],
        meta["model"],
        status_code,
    )

def _log_fallback(operation: str, meta: dict, reason: str):
    logger.warning(
        "LLM fallback operation=%s provider=%s tier=%s model=%s reason=%s",
        operation,
        meta["provider"],
        meta["tier"],
        meta["model"],
        reason,
    )

def _build_messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    return [{"role": "system", "content": system_prompt}, *messages]

def _response_error(response, meta: dict) -> RuntimeError:
    provider_name = meta["provider"]
    if response.status_code == 429:
        return RuntimeError(f"{provider_name} rate limit — retry in a moment")
    return RuntimeError(f"{provider_name} {response.status_code}: {response.text[:300]}")

def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600

def _ensure_tier(tier: str) -> str:
    if tier not in DEFAULT_MODEL_CHAINS:
        raise RuntimeError(f"Unknown model tier '{tier}'")
    return tier

# ── Subprocess provider (CC / Codex) ─────────────────────────────────────────

def _format_messages_for_cli(messages: list[dict]) -> str:
    """Format multi-turn history into a single prompt for --print mode."""
    if len(messages) == 1:
        return messages[0].get("content", "")
    prior = []
    for m in messages[:-1]:
        label = "Human" if m.get("role") == "user" else "Assistant"
        prior.append(f"{label}: {m.get('content', '')}")
    history = "\n\n".join(prior)
    last = messages[-1].get("content", "")
    return f"[Prior conversation:]\n{history}\n\n[Current message:]\n{last}"

async def _stream_subprocess(
    candidate: dict,
    messages: list[dict],
    system_prompt: str,
) -> AsyncGenerator[str, None]:
    """Run CC/Codex via subprocess --print, yield the full response as one chunk."""
    p = _provider(candidate["provider"])
    command = p.get("command", "claude")
    model = candidate["model"]
    prompt = _format_messages_for_cli(messages)

    logger.warning(
        "subprocess request provider=%s model=%s command=%s",
        candidate["provider"], model, command,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            command, "--print",
            "--model", model,
            "--system-prompt", system_prompt,
            "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except FileNotFoundError:
        raise RuntimeError(f"{command} not found — is Claude Code installed?")
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{command} subprocess timed out after 120s")

    if proc.returncode != 0:
        err = stderr.decode()[:300]
        raise RuntimeError(f"{command} exited {proc.returncode}: {err}")

    text = stdout.decode().strip()
    if text:
        yield text

# ── Public API ────────────────────────────────────────────────────────────────

async def stream_chat(
    messages: list[dict],
    system_prompt: str,
    model_tier: str = "medium",
) -> AsyncGenerator[str, None]:
    """Stream a chat response, yielding clean text chunks (SSE parsed server-side)."""
    model_tier = _ensure_tier(model_tier)
    candidates = _available_candidates(model_tier)
    last_error = None
    async with httpx.AsyncClient() as client:
        for index, candidate in enumerate(candidates, start=1):
            # Subprocess providers (CC, Codex) bypass HTTP
            if _provider(candidate["provider"]).get("type") == "subprocess":
                try:
                    async for chunk in _stream_subprocess(candidate, messages, system_prompt):
                        yield chunk
                    return
                except Exception as exc:
                    last_error = exc
                    if index < len(candidates):
                        logger.warning("subprocess fallback provider=%s reason=%s", candidate["provider"], exc)
                        continue
                    raise last_error

            meta = _log_request_start("stream_chat", model_tier, candidate, messages, stream=True, attempt=index, total=len(candidates))
            try:
                async with client.stream(
                    "POST",
                    _provider(candidate["provider"])["url"],
                    headers=_headers(candidate["provider"]),
                    json={
                        "model":      candidate["model"],
                        "messages":   _build_messages(system_prompt, messages),
                        "stream":     True,
                        "max_tokens": 1024,
                    },
                    timeout=60,
                ) as response:
                    _log_response_status("stream_chat", meta, response.status_code)
                    if response.status_code != 200:
                        body = await response.aread()
                        last_error = RuntimeError(
                            f"{meta['provider']} {response.status_code}: {body.decode()[:300]}"
                        )
                        if _is_retryable_status(response.status_code) and index < len(candidates):
                            _log_fallback("stream_chat", meta, f"http_{response.status_code}")
                            continue
                        raise last_error
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[len("data:"):].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                            text = chunk["choices"][0]["delta"].get("content") or ""
                            if text:
                                yield text
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                    return
            except httpx.HTTPError as exc:
                last_error = RuntimeError(f"{meta['provider']} request failed: {exc}")
                if index < len(candidates):
                    _log_fallback("stream_chat", meta, exc.__class__.__name__)
                    continue
                raise last_error
    if last_error:
        raise last_error


async def complete_chat(
    messages: list[dict],
    system_prompt: str,
    model_tier: str = "medium",
) -> str:
    """Non-streaming completion — use for structured extraction."""
    model_tier = _ensure_tier(model_tier)
    candidates = _available_candidates(model_tier)
    last_error = None
    async with httpx.AsyncClient() as client:
        for index, candidate in enumerate(candidates, start=1):
            if _provider(candidate["provider"]).get("type") == "subprocess":
                try:
                    chunks = [c async for c in _stream_subprocess(candidate, messages, system_prompt)]
                    return "".join(chunks)
                except Exception as exc:
                    last_error = exc
                    if index < len(candidates):
                        logger.warning("subprocess fallback provider=%s reason=%s", candidate["provider"], exc)
                        continue
                    raise last_error

            meta = _log_request_start("complete_chat", model_tier, candidate, messages, stream=False, attempt=index, total=len(candidates))
            try:
                response = await client.post(
                    _provider(candidate["provider"])["url"],
                    headers=_headers(candidate["provider"]),
                    json={
                        "model":      candidate["model"],
                        "messages":   _build_messages(system_prompt, messages),
                        "stream":     False,
                        "max_tokens": 1024,
                    },
                    timeout=60,
                )
            except httpx.HTTPError as exc:
                last_error = RuntimeError(f"{meta['provider']} request failed: {exc}")
                if index < len(candidates):
                    _log_fallback("complete_chat", meta, exc.__class__.__name__)
                    continue
                raise last_error
            _log_response_status("complete_chat", meta, response.status_code)
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]
            last_error = _response_error(response, meta)
            if _is_retryable_status(response.status_code) and index < len(candidates):
                _log_fallback("complete_chat", meta, f"http_{response.status_code}")
                continue
            raise last_error
    if last_error:
        raise last_error
    raise RuntimeError("LLM completion failed with no available candidates")


async def chat_with_tools(
    messages: list[dict],
    system_prompt: str,
    tools: list[dict],
    model_tier: str = "medium",
) -> dict:
    """
    Function-calling completion. Returns either:
      {"type": "text",      "content": "..."}
      {"type": "tool_call", "name": "...", "arguments": {...}}
    """
    model_tier = _ensure_tier(model_tier)
    candidates = _available_candidates(model_tier)
    last_error = None
    async with httpx.AsyncClient() as client:
        for index, candidate in enumerate(candidates, start=1):
            meta = _log_request_start("chat_with_tools", model_tier, candidate, messages, stream=False, attempt=index, total=len(candidates))
            try:
                response = await client.post(
                    _provider(candidate["provider"])["url"],
                    headers=_headers(candidate["provider"]),
                    json={
                        "model":      candidate["model"],
                        "messages":   _build_messages(system_prompt, messages),
                        "tools":      tools,
                        "stream":     False,
                        "max_tokens": 1024,
                    },
                    timeout=60,
                )
            except httpx.HTTPError as exc:
                last_error = RuntimeError(f"{meta['provider']} request failed: {exc}")
                if index < len(candidates):
                    _log_fallback("chat_with_tools", meta, exc.__class__.__name__)
                    continue
                raise last_error
            _log_response_status("chat_with_tools", meta, response.status_code)
            if response.status_code != 200:
                last_error = _response_error(response, meta)
                if _is_retryable_status(response.status_code) and index < len(candidates):
                    _log_fallback("chat_with_tools", meta, f"http_{response.status_code}")
                    continue
                raise last_error

            message = response.json()["choices"][0]["message"]
            if message.get("tool_calls"):
                call = message["tool_calls"][0]["function"]
                return {
                    "type":      "tool_call",
                    "name":      call["name"],
                    "arguments": json.loads(call["arguments"]),
                }

            return {"type": "text", "content": message.get("content", "")}
    if last_error:
        raise last_error
    raise RuntimeError("LLM tool call failed with no available candidates")
