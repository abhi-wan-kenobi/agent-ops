"""Provider layer: plain-HTTP OpenAI-compatible chat completions over urllib.

This replaces the machine-welded transport of the original panel (a local gateway plus a
`claude -p` subprocess per seat). Talking HTTP directly is a portability requirement, not a
style choice: the plugin must work from any host, with nothing but an endpoint and an API
key the user owns.

The registry is keyed by the provider `type` in panel.toml. The shipped types
(openrouter, ollama, openai-compatible) all speak the OpenAI chat-completions dialect.
Anything else is one subclass of BaseProvider: a `type` of the form `module:ClassName`
loads it, so a provider is a file the user owns rather than a fork of this package
(docs/PROVIDERS.md).
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

from . import __version__
from .classify import SECRET_RE
from .config import RESERVED_PARAMS, ConfigError, ProviderConfig, home

USER_AGENT = f"agent-ops/{__version__}"

# Drop-in home for custom provider modules, under AGENT_OPS_HOME (see _load_custom_class).
CUSTOM_PROVIDER_DIR = "providers"

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
    """OpenAI chat-completions transport: auth, retries, timeouts, response triage.

    A provider that speaks another dialect overrides only the four dialect hooks —
    `chat_path`, `build_body`, `parse_response`, and (when the endpoint has no
    OpenAI-style listing) `list_models` — and inherits the retry and error handling.
    """

    chat_path = "/chat/completions"

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
        # Config-supplied headers (attribution / routing / tenancy) sit between the
        # defaults and auth: they may override the User-Agent, but Authorization is
        # applied AFTER them and Content-Type/Authorization are refused at config load,
        # so no config line can smuggle or displace a credential.
        h.update(self.cfg.headers)
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        req = urllib.request.Request(f"{self.cfg.base_url}{path}",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return json.loads(f.read().decode("utf-8", "replace"))

    # -- dialect hooks -----------------------------------------------------------------

    def build_body(self, model: str, messages: list[dict], *, max_tokens: int,
                   temperature: float | None) -> dict:
        """The JSON request body for one seat call. An unset temperature is omitted, not
        defaulted: a default here would silently override the model's own."""
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            body["temperature"] = temperature
        return body

    def parse_response(self, data: dict) -> dict:
        """One HTTP-200 JSON body -> SeatOutput fields: `content`, `reasoning` and
        `finish_reason`, or `error=` for a provider-side failure the body reports.

        Indexing or attribute errors on a body of the wrong shape (KeyError, IndexError,
        AttributeError, TypeError, ValueError) need no handling here: call() turns them
        into a 'malformed response shape' seat error rather than crashing the panel."""
        # An OpenAI-compatible gateway can answer 200 with an error envelope and no
        # choices at all — OpenRouter does exactly this when the upstream provider it
        # routed to fails. Measured 2026-09-08: 2 of 6 identical probe calls to
        # z-ai/glm-5.3-flash came back that way. Falling through to `{}` here turned
        # the provider's own explanation into "empty content", which reads as a dead
        # seat and sends the operator hunting the model instead of the route. Silent
        # absence, in the tool whose first playbook rule is about silent absence.
        if not data.get("choices") and data.get("error") is not None:
            err = data["error"]
            detail = err.get("message") if isinstance(err, dict) else str(err)
            detail = SECRET_RE.sub("[REDACTED]", str(detail or "").strip())[:200]
            return {"error": f"provider error: {detail}" if detail
                    else "provider error (no message)"}
        ch = (data.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        return {
            "content": str(msg.get("content") or ""),
            "reasoning": str(msg.get("reasoning_content") or msg.get("reasoning") or ""),
            "finish_reason": str(ch.get("finish_reason") or ""),
        }

    def lists_model(self, model: str, listed: set[str]) -> bool:
        """Does the provider's `list_models()` id set cover this seat's model id? Exact
        match by default; a provider whose listing spells ids differently from requests
        overrides it."""
        return model in listed

    def confirm_unlisted(self, model: str, timeout: float = 15.0) -> bool:
        """Called only for a seat `lists_model` rejected: can the provider positively say
        it serves this model anyway? True keeps the seat, False (the default) drops it as
        NOT ROUTABLE. A listing is authoritative unless the provider knows better; one
        that lists only part of what it serves overrides this with a cheap targeted
        probe — never a chat call, which would spend the seat's budget."""
        return False

    # -- API ---------------------------------------------------------------------------

    def call(self, model: str, messages: list[dict], *, max_tokens: int,
             temperature: float | None = None, timeout: float = 900.0,
             params: dict | None = None) -> SeatOutput:
        """One chat completion. Never raises for transport problems — the panel must keep
        running its other seats — so every failure lands in SeatOutput.error instead.

        `params` (a seat's config `params`) goes in FIRST and the client-owned fields that
        `build_body` produces are written over it, so even a params dict that bypassed
        load_config's refusal cannot change the model, messages or output budget — in any
        dialect. It only ever reaches the body, never the headers, so auth stays where
        _headers puts it."""
        body = self.build_body(model, messages, max_tokens=max_tokens,
                               temperature=temperature)
        if params:
            body = {**{k: v for k, v in params.items() if k not in RESERVED_PARAMS}, **body}

        started = time.monotonic()

        def out(**kw) -> SeatOutput:
            return SeatOutput(seconds=round(time.monotonic() - started, 1), **kw)

        attempt = 0
        budget = MAX_ATTEMPTS
        while True:
            attempt += 1
            try:
                data = self._post(self.chat_path, body, timeout)
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
                return out(**self.parse_response(data))
            except (AttributeError, IndexError, KeyError, TypeError, ValueError) as e:
                return out(error=f"malformed response shape: {type(e).__name__}")

    def list_models(self, timeout: float = 15.0) -> list[dict] | None:
        """Best-effort GET /models. None = could not ask — which must never be treated as
        "nothing exists"; an unreachable listing endpoint is not a verdict on the models.

        The blanket `except` is a recorded decision (dogfood finding F, 2026-09-02), not
        an oversight: a 401, an SSL failure and a network drop all collapse to the same
        None DELIBERATELY, because every caller treats None identically ("could not ask,
        do not block") and the failure that matters — a bad key — surfaces loudly moments
        later on the POST that actually runs the seat. Distinguishing the causes here
        would add taxonomy with no consumer."""
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


class OllamaProvider(OpenAICompatProvider):
    """Ollama — the local daemon or the hosted API (https://ollama.com/v1 with a key).

    Same dialect; different model naming. Its listing spells every tag out
    (`llama3.1:latest`) while a request may leave the tag off and be served `:latest`.
    Without knowing that, preflight reports a model Ollama would happily serve as NOT
    ROUTABLE — which is what the bare-name starter panel from `init --ollama` hit."""

    def lists_model(self, model: str, listed: set[str]) -> bool:
        return super().lists_model(model, listed) or (
            ":" not in model and f"{model}:latest" in listed)

    def confirm_unlisted(self, model: str, timeout: float = 15.0) -> bool:
        """A local daemon's /v1/models lists only what was `ollama pull`ed, but it serves
        any real cloud model on demand — measured 2026-09-30 on 0.31.2: `glm-5.3-flash:cloud`
        and `deepseek-v4.1-flash:cloud` unlisted, both answered a chat call. Trusting the
        listing alone reported them NOT ROUTABLE. `/api/show` is the discriminator: 200 for
        a real cloud id, 404 for an unpulled local model or an id that does not exist. Any
        failure to ask counts as unconfirmed — the pre-existing NOT ROUTABLE verdict."""
        root = self.cfg.base_url.removesuffix("/v1")
        req = urllib.request.Request(
            f"{root}/api/show", data=json.dumps({"model": model}).encode("utf-8"),
            headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as f:
                return f.status == 200
        except Exception:                                     # noqa: BLE001 — best effort
            return False


class OpenRouterProvider(OpenAICompatProvider):
    """Same dialect; adds OpenRouter's optional attribution headers."""

    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        h.setdefault("X-Title", "agent-ops")
        return h


PROVIDER_TYPES: dict[str, type[BaseProvider]] = {
    "openrouter": OpenRouterProvider,
    "ollama": OllamaProvider,
    "openai-compatible": OpenAICompatProvider,
}


def _load_custom_class(cfg: ProviderConfig) -> type[BaseProvider]:
    """Resolve `type = "module:ClassName"` to a BaseProvider subclass.

    Modules are found on PYTHONPATH and in `<AGENT_OPS_HOME>/providers/`, a directory the
    user owns — appended, never prepended, so a file dropped there cannot shadow the
    standard library or this package. Every failure is a ConfigError naming the fix: it
    happens before any seat runs, where a traceback would read as a broken tool."""
    where = f"provider {cfg.name!r} (type {cfg.type!r})"
    module_name, _, attr = cfg.type.partition(":")
    # Every dotted part must be an identifier: a leading dot is a relative import that
    # needs a package (an unreadable TypeError), and a slash is a file path pretending to
    # be a module. Neither can escape the import system, but both deserve the shape hint.
    if (not all(part.isidentifier() for part in module_name.split("."))
            or not attr.isidentifier()):
        raise ConfigError(f"{where}: a custom type is 'module:ClassName', "
                          f"e.g. 'my_provider:MyProvider'")
    drop_in = home() / CUSTOM_PROVIDER_DIR
    if drop_in.is_dir() and str(drop_in) not in sys.path:
        sys.path.append(str(drop_in))
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        # Only "your module is missing" when the missing module IS the requested one; a
        # provider that imports something absent must say what, not blame its own path.
        if e.name and (module_name == e.name or module_name.startswith(e.name + ".")):
            raise ConfigError(f"{where}: no module {module_name!r} — put "
                              f"{module_name.split('.')[0]}.py in {drop_in} or on "
                              f"PYTHONPATH") from e
        raise ConfigError(f"{where}: importing {module_name!r} failed: {e}") from e
    except Exception as e:                       # noqa: BLE001 — user code ran at import
        raise ConfigError(f"{where}: importing {module_name!r} failed: "
                          f"{type(e).__name__}: {e}") from e
    cls = getattr(module, attr, None)
    if not (isinstance(cls, type) and issubclass(cls, BaseProvider)):
        raise ConfigError(f"{where}: {module_name}.{attr} must be a class deriving from "
                          f"agent_ops.providers.BaseProvider")
    return cls


def make_provider(cfg: ProviderConfig) -> BaseProvider:
    cls = PROVIDER_TYPES.get(cfg.type)
    if cls is not None:
        return cls(cfg)
    if ":" not in cfg.type:
        raise ConfigError(
            f"provider {cfg.name!r} has unknown type {cfg.type!r} — "
            f"known types: {', '.join(sorted(PROVIDER_TYPES))}, or 'module:ClassName' "
            f"for a custom provider (see docs/PROVIDERS.md)")
    custom = _load_custom_class(cfg)
    try:
        return custom(cfg)
    except ConfigError:
        raise
    except Exception as e:                       # noqa: BLE001 — user code in __init__
        raise ConfigError(f"provider {cfg.name!r} (type {cfg.type!r}) failed to "
                          f"initialise: {type(e).__name__}: {e}") from e


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
