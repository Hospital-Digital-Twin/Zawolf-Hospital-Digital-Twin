"""Optional LLM connection (Groq Cloud, OpenAI-compatible chat API), configured only through environment
variables (or a local .env file loaded at start-up):

    GROQ_API_KEY            required to enable the LLM; never sent to the browser, never logged
    GROQ_MODEL              default openai/gpt-oss-120b
    GROQ_BASE_URL           default https://api.groq.com/openai/v1
    GROQ_TIMEOUT_S          per request, default 20
    GROQ_MAX_TOKENS         answer limit per call, default 500
    GROQ_MAX_CALLS_PER_MIN  local budget, default 20

The LLM is only ever used for wording and for explaining numbers the backend already computed.
Calls retry with backoff on 429 / 5xx / timeouts (honouring Retry-After), identical requests are
served from a short cache, a per-minute budget protects the quota, and every failure is reduced to
a safe category: no response body, key or header is ever surfaced or logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .explain import LLMExplainer, TemplateExplainer

DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_BASE = "https://api.groq.com/openai/v1"
MAX_INPUT_CHARS = 8000
CACHE_TTL_S, CACHE_MAX = 600, 128
RETRIES, BACKOFF_S, RETRY_AFTER_CAP_S = 3, 1.0, 15.0

MESSAGES = {
    "not_configured": "No API key is set (GROQ_API_KEY).",
    "auth": "The API key was rejected by the provider.",
    "model": "The model was not found or this key has no access to it.",
    "rate_limit": "The provider's rate limit was reached.",
    "timeout": "The provider did not answer in time.",
    "network": "The provider could not be reached.",
    "server": "The provider reported an internal error.",
    "bad_response": "The provider returned an unexpected response.",
    "budget": "The local call budget for this minute is used up.",
}


class LLMError(Exception):
    def __init__(self, category: str, retry_after: float | None = None) -> None:
        super().__init__(MESSAGES.get(category, category))
        self.category, self.retry_after = category, retry_after

    @property
    def message(self) -> str:
        return MESSAGES.get(self.category, self.category)


def load_env(path: str | Path) -> list[str]:
    """Read KEY=VALUE lines from a .env file into os.environ without overriding real environment variables."""
    p, loaded = Path(path), []
    if not p.exists():
        return loaded
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v
            loaded.append(k)
    return loaded


def config() -> dict:
    env = os.environ
    key = env.get("GROQ_API_KEY", "").strip()
    return {"provider": "Groq Cloud", "configured": bool(key), "model": env.get("GROQ_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            "base_url": env.get("GROQ_BASE_URL", DEFAULT_BASE).rstrip("/"), "timeout": float(env.get("GROQ_TIMEOUT_S", 20)),
            "max_tokens": int(env.get("GROQ_MAX_TOKENS", 500)), "max_calls_per_min": int(env.get("GROQ_MAX_CALLS_PER_MIN", 20)), "_key": key}


class Client:
    """Process-wide Groq client: retries, cache, budget and counters. Safe to call from several threads."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.sleep, self.clock = sleep, clock
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[float, str]] = {}
        self.recent: deque[float] = deque()
        self.stats = {"calls": 0, "retries": 0, "failures": 0, "cache_hits": 0, "last_error": None, "last_ok": None}

    # ── transport ──
    def _request(self, cfg: dict, path: str, body: bytes | None) -> dict:
        req = urllib.request.Request(cfg["base_url"] + path, data=body, method="POST" if body else "GET",
                                     headers={"Authorization": f"Bearer {cfg['_key']}", "Content-Type": "application/json", "User-Agent": "hospital-digital-twin"})
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            retry_after = None
            try:
                retry_after = float(e.headers.get("Retry-After")) if e.headers.get("Retry-After") else None
            except ValueError:
                pass
            category = {401: "auth", 403: "auth", 404: "model", 429: "rate_limit"}.get(e.code, "server" if e.code >= 500 else "bad_response")
            raise LLMError(category, retry_after) from None
        except TimeoutError:
            raise LLMError("timeout") from None
        except urllib.error.URLError as e:
            raise LLMError("timeout" if isinstance(getattr(e, "reason", None), TimeoutError) else "network") from None
        except (json.JSONDecodeError, ValueError):
            raise LLMError("bad_response") from None

    def _with_retries(self, cfg: dict, path: str, body: bytes | None) -> dict:
        for attempt in range(RETRIES):
            try:
                return self._request(cfg, path, body)
            except LLMError as e:
                if e.category not in ("rate_limit", "server", "timeout", "network") or attempt == RETRIES - 1:
                    raise
                self.stats["retries"] += 1
                delay = min(e.retry_after, RETRY_AFTER_CAP_S) if e.retry_after is not None else BACKOFF_S * 2 ** attempt
                self.sleep(delay)
        raise LLMError("server")                                     # unreachable; keeps the type checker honest

    def _budget(self, limit: int) -> None:
        now = self.clock()
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()
        if len(self.recent) >= limit:
            raise LLMError("budget")
        self.recent.append(now)

    # ── public ──
    def chat(self, system: str, user: str, max_tokens: int | None = None) -> str:
        cfg = config()
        if not cfg["configured"]:
            raise LLMError("not_configured")
        system, user = system[:MAX_INPUT_CHARS], user[:MAX_INPUT_CHARS]
        limit = min(max_tokens or cfg["max_tokens"], cfg["max_tokens"])
        key = hashlib.sha256(f"{cfg['model']}|{limit}|{system}|{user}".encode()).hexdigest()
        with self.lock:
            hit = self.cache.get(key)
            if hit and self.clock() - hit[0] < CACHE_TTL_S:
                self.stats["cache_hits"] += 1
                return hit[1]
            self._budget(cfg["max_calls_per_min"])
            self.stats["calls"] += 1
        body = json.dumps({"model": cfg["model"], "temperature": 0.1, "max_tokens": limit,
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
        try:
            data = self._with_retries(cfg, "/chat/completions", body)
            text = data["choices"][0]["message"]["content"] or ""
        except LLMError as e:
            self._failed(e.category)
            raise
        except (KeyError, IndexError, TypeError):
            self._failed("bad_response")
            raise LLMError("bad_response") from None
        with self.lock:
            if len(self.cache) >= CACHE_MAX:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (self.clock(), text)
            self.stats["last_ok"] = datetime.now(timezone.utc).isoformat()
        return text

    def _failed(self, category: str) -> None:
        with self.lock:
            self.stats["failures"] += 1
            self.stats["last_error"] = category

    def test(self) -> dict:
        """A real round trip: list the models available to the key (cheap, uses no tokens)."""
        cfg = config()
        if not cfg["configured"]:
            return {"ok": False, "category": "not_configured", "message": MESSAGES["not_configured"], "model": cfg["model"]}
        started = self.clock()
        try:
            data = self._with_retries(cfg, "/models", None)
        except LLMError as e:
            self._failed(e.category)
            return {"ok": False, "category": e.category, "message": e.message, "model": cfg["model"], "latency_ms": round((self.clock() - started) * 1000)}
        ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        found = cfg["model"] in ids
        with self.lock:
            if found:
                self.stats["last_ok"] = datetime.now(timezone.utc).isoformat()
        return {"ok": found, "category": "ok" if found else "model", "model": cfg["model"], "models_available": len(ids),
                "message": "Connected." if found else MESSAGES["model"], "latency_ms": round((self.clock() - started) * 1000)}


client = Client()


def completion() -> Callable[[str, str], Awaitable[str]] | None:
    """Async (system, user) -> text, or None when no key is configured."""
    if not config()["configured"]:
        return None
    return lambda system, user: asyncio.to_thread(client.chat, system, user)


def explainer():
    """LLM-worded summary when a key is configured, otherwise the deterministic template."""
    complete = completion()
    return LLMExplainer(complete) if complete else TemplateExplainer()


def status() -> dict:
    cfg = config()
    return {"provider": cfg["provider"], "model": cfg["model"], "configured": cfg["configured"], "stats": dict(client.stats),
            "limits": {"timeout_s": cfg["timeout"], "max_tokens": cfg["max_tokens"], "max_calls_per_min": cfg["max_calls_per_min"]},
            "env": ["GROQ_API_KEY", "GROQ_MODEL", "GROQ_BASE_URL", "GROQ_TIMEOUT_S", "GROQ_MAX_TOKENS", "GROQ_MAX_CALLS_PER_MIN"]}
