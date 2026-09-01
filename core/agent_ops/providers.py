"""Provider layer: plain-HTTP OpenAI-compatible chat completions over urllib.

This replaces the machine-welded transport of the original panel (a local gateway plus a
`claude -p` subprocess per seat). Talking HTTP directly is a portability requirement, not a
style choice: the plugin must work from any host, with nothing but an endpoint and an API
key the user owns.

The registry is keyed by the provider `type` in panel.toml. Both shipped types speak the
same OpenAI-compatible dialect today; a future hosted/metered provider is one new subclass
and one registry entry, no restructuring.
"""
from __future__ import annotations

import dataclasses
import json
import os
import socket
import time
import urllib.error
import urllib.request

from .classify import SECRET_RE
from .config import ConfigError, ProviderConfig

USER_AGENT = "agent-ops/0.1"

# Transient failures are retried with exponential backoff; a hard client error is not.
# 429 gets a single retry: one backoff is polite, hammering a rate limit is not.
MAX_ATTEMPTS = 3
RETRY_429_ATTEMPTS = 2
BACKOFF_BASE_S = 1.5


@dataclasses.dataclass
class SeatOutput:
    """One seat's raw result. `error` is None for any HTTP 200 with parseable JSON —
    an empty `content` is NOT an error here; classify_seat is what judges emptiness."""
    content: str = ""
    reasoning: str = ""
    finish_reason: str = ""
    error: str | None = None            # "timeout" | human-readable reason
    seconds: float = 0.0


class BaseProvider:
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.api_key: str | None = None
        if cfg.api_key_env:
            self.api_key = os.environ.get(cfg.api_key_env)
            if not self.api_key:
                # A configured-but-absent key is a config error BEFORE any seat runs,
                # never a per-seat failure that reads as a flaky panel.
                raise ConfigError(
                    f"provider {cfg.name!r} needs the environment variable "
                    f"{cfg.api_key_env} and it is not set")

    # -- request plumbing --------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        req = urllib.request.Request(f"{self.cfg.base_url}{path}",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return json.loads(f.read().decode("utf-8", "replace"))

    # -- API ---------------------------------------------------------------------------

    def call(self, model: str, messages: list[dict], *, max_tokens: int,
             temperature: float | None = None, timeout: float = 900.0) -> SeatOutput:
        """One chat completion. Never raises for transport problems — the panel must keep
        running its other seats — so every failure lands in SeatOutput.error instead."""
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            body["temperature"] = temperature

        started = time.monotonic()

        def out(**kw) -> SeatOutput:
            return SeatOutput(seconds=round(time.monotonic() - started, 1), **kw)

        attempt = 0
        budget = MAX_ATTEMPTS
        while True:
            attempt += 1
            try:
                data = self._post("/chat/completions", body, timeout)
            except urllib.error.HTTPError as e:
                reason = _http_reason(e)
                if e.code == 429 and attempt < RETRY_429_ATTEMPTS:
                    time.sleep(BACKOFF_BASE_S * attempt)
                    continue
                if e.code >= 500 and attempt < budget:
                    time.sleep(BACKOFF_BASE_S * attempt)
                    continue
                return out(error=reason)
            except (TimeoutError, socket.timeout):
                # The seat's whole time budget is gone; there is nothing to retry with.
                return out(error="timeout")
            except urllib.error.URLError as e:
                if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
                    return out(error="timeout")
                if attempt < budget:
                    time.sleep(BACKOFF_BASE_S * attempt)
                    continue
                return out(error=f"unreachable: {getattr(e, 'reason', e)}")
            except (json.JSONDecodeError, ValueError) as e:
                return out(error=f"malformed response: {e}")
            except OSError as e:
                if attempt < budget:
                    time.sleep(BACKOFF_BASE_S * attempt)
                    continue
                return out(error=f"transport error: {type(e).__name__}")

            try:
                ch = (data.get("choices") or [{}])[0]
                msg = ch.get("message") or {}
                return out(
                    content=str(msg.get("content") or ""),
                    reasoning=str(msg.get("reasoning_content") or msg.get("reasoning") or ""),
                    finish_reason=str(ch.get("finish_reason") or ""),
                )
            except (AttributeError, IndexError, TypeError) as e:
                return out(error=f"malformed response shape: {type(e).__name__}")

    def list_models(self, timeout: float = 15.0) -> list[dict] | None:
        """Best-effort GET /models. None = could not ask — which must never be treated as
        "nothing exists"; an unreachable listing endpoint is not a verdict on the models."""
        req = urllib.request.Request(f"{self.cfg.base_url}/models",
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as f:
                data = json.loads(f.read().decode("utf-8", "replace"))
            rows = data.get("data")
            return rows if isinstance(rows, list) else None
        except Exception:                                     # noqa: BLE001 — best effort
            return None

    def model_ids(self, timeout: float = 15.0) -> set[str] | None:
        rows = self.list_models(timeout=timeout)
        if rows is None:
            return None
        return {str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")}

    def context_lengths(self, timeout: float = 15.0) -> dict[str, int]:
        """model id -> context length, for endpoints that advertise it (OpenRouter does).
        Empty dict when the endpoint doesn't say — unknown is not the same as narrow."""
        rows = self.list_models(timeout=timeout) or []
        out: dict[str, int] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            top = r.get("top_provider")
            ctx = r.get("context_length")
            if not isinstance(ctx, int) and isinstance(top, dict):
                ctx = top.get("context_length")
            if isinstance(ctx, int) and ctx > 0 and r.get("id"):
                out[str(r["id"])] = ctx
        return out


class OpenAICompatProvider(BaseProvider):
    """The generic OpenAI-compatible client. Ollama's /v1 endpoint speaks this dialect;
    so does anything else that clones it."""


class OpenRouterProvider(OpenAICompatProvider):
    """Same dialect; adds OpenRouter's optional attribution headers."""

    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        h.setdefault("X-Title", "agent-ops")
        return h


PROVIDER_TYPES: dict[str, type[BaseProvider]] = {
    "openrouter": OpenRouterProvider,
    "ollama": OpenAICompatProvider,
    "openai-compatible": OpenAICompatProvider,
}


def make_provider(cfg: ProviderConfig) -> BaseProvider:
    cls = PROVIDER_TYPES.get(cfg.type)
    if cls is None:
        raise ConfigError(
            f"provider {cfg.name!r} has unknown type {cfg.type!r} — "
            f"known types: {', '.join(sorted(PROVIDER_TYPES))}")
    return cls(cfg)


def _http_reason(e: urllib.error.HTTPError) -> str:
    """Extract the server's own message when it sent one; fall back to the status line."""
    try:
        raw = e.read().decode("utf-8", "replace")[:500]
    except OSError:
        raw = ""
    detail = ""
    try:
        err = json.loads(raw).get("error")
        detail = err.get("message") if isinstance(err, dict) else str(err or "")
    except (json.JSONDecodeError, AttributeError, ValueError):
        detail = raw.strip()
    detail = (detail or "").strip()
    # The outbound payload is secret-gated; error text coming BACK was not, and it lands
    # in seat reports and stderr verbatim. An endpoint that echoes request context into
    # its error body would put a live credential on disk. Same regex, inbound direction.
    # Dogfood finding, 2026-09-01.
    detail = SECRET_RE.sub("[REDACTED]", detail)
    return f"HTTP {e.code}" + (f": {detail[:200]}" if detail else "")
