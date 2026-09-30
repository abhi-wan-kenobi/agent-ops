"""Provider layer against a real local HTTP server — the round trip the panel rides on.

A live socket beats monkeypatching urllib: the retry, timeout and auth behaviour being
pinned here is exactly the part that only shows up on a real connection.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_ops.config import ConfigError, ProviderConfig
from agent_ops import providers as prov_mod
from agent_ops.providers import (OpenAICompatProvider, OpenRouterProvider, SeatOutput,
                                 make_provider)


class _Script:
    """Per-test scripted responses. Each entry: (status, body_bytes) or ("hang", seconds)."""

    def __init__(self):
        self.responses = []
        self.requests = []          # (path, headers, parsed_body)
        self.lock = threading.Lock()

    def next(self):
        with self.lock:
            return self.responses.pop(0) if self.responses else (200, _ok_body("fallback"))


def _ok_body(content: str, reasoning: str = "", finish: str = "stop") -> bytes:
    msg = {"content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return json.dumps({"choices": [{"message": msg, "finish_reason": finish}]}).encode()


@pytest.fixture()
def server():
    script = _Script()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            script.requests.append((self.path, dict(self.headers), body))
            status, payload = script.next()
            if status == "hang":
                import time
                time.sleep(payload)
                status, payload = 200, _ok_body("late")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            script.requests.append((self.path, dict(self.headers), None))
            status, payload = script.next()
            self.send_response(status)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):                            # keep pytest output clean
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    script.base_url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    yield script
    httpd.shutdown()


def _provider(script, *, key_env=None, type_="openai-compatible"):
    return make_provider(ProviderConfig(name="test", type=type_,
                                        base_url=script.base_url, api_key_env=key_env))


def _fast_retries(monkeypatch):
    monkeypatch.setattr(prov_mod, "BACKOFF_BASE_S", 0.01)


MSGS = [{"role": "user", "content": "hi"}]


def test_success_round_trip_carries_content_and_reasoning(server):
    server.responses = [(200, _ok_body("the report", reasoning="thoughts"))]
    out = _provider(server).call("m1", MSGS, max_tokens=100)
    assert out.error is None
    assert out.content == "the report"
    assert out.reasoning == "thoughts"
    assert out.finish_reason == "stop"
    path, headers, body = server.requests[0]
    assert path == "/v1/chat/completions"
    assert body["model"] == "m1" and body["max_tokens"] == 100
    assert "temperature" not in body, "unset temperature must be omitted, not defaulted"
    assert "Authorization" not in headers, "no api_key_env means no auth header"


def test_temperature_is_sent_when_given(server):
    server.responses = [(200, _ok_body("x"))]
    _provider(server).call("m1", MSGS, max_tokens=10, temperature=1.0)
    assert server.requests[0][2]["temperature"] == 1.0


def test_bearer_auth_header_from_env(server, monkeypatch):
    monkeypatch.setenv("TEST_PANEL_KEY", "sekret-token-value")
    server.responses = [(200, _ok_body("x"))]
    _provider(server, key_env="TEST_PANEL_KEY").call("m1", MSGS, max_tokens=10)
    assert server.requests[0][1]["Authorization"] == "Bearer sekret-token-value"


def test_missing_configured_env_var_is_a_hard_config_error(server, monkeypatch):
    monkeypatch.delenv("TEST_PANEL_KEY", raising=False)
    with pytest.raises(ConfigError, match="TEST_PANEL_KEY"):
        _provider(server, key_env="TEST_PANEL_KEY")


def test_unknown_provider_type_is_a_config_error(server):
    with pytest.raises(ConfigError, match="unknown type"):
        make_provider(ProviderConfig(name="x", type="carrier-pigeon",
                                     base_url=server.base_url))


def test_5xx_is_retried_then_succeeds(server, monkeypatch):
    _fast_retries(monkeypatch)
    server.responses = [(500, b"boom"), (502, b"boom"), (200, _ok_body("recovered"))]
    out = _provider(server).call("m1", MSGS, max_tokens=10)
    assert out.error is None and out.content == "recovered"
    assert len(server.requests) == 3


def test_5xx_exhausting_retries_reports_the_status(server, monkeypatch):
    _fast_retries(monkeypatch)
    server.responses = [(500, b"a"), (500, b"b"), (500, b"c"), (500, b"d")]
    out = _provider(server).call("m1", MSGS, max_tokens=10)
    assert out.error is not None and "500" in out.error
    assert len(server.requests) == 3, "exactly MAX_ATTEMPTS tries, no more"


def test_429_gets_a_single_retry(server, monkeypatch):
    _fast_retries(monkeypatch)
    server.responses = [(429, b'{"error":{"message":"slow down"}}'),
                        (429, b'{"error":{"message":"slow down"}}'),
                        (200, _ok_body("never reached"))]
    out = _provider(server).call("m1", MSGS, max_tokens=10)
    assert out.error is not None and "429" in out.error and "slow down" in out.error
    assert len(server.requests) == 2, "429 retries once, then gives up"


def test_other_4xx_is_not_retried(server, monkeypatch):
    _fast_retries(monkeypatch)
    server.responses = [(400, b'{"error":{"message":"bad model name"}}')]
    out = _provider(server).call("m1", MSGS, max_tokens=10)
    assert out.error is not None and "400" in out.error and "bad model name" in out.error
    assert len(server.requests) == 1


def test_read_timeout_maps_to_timeout_error(server):
    server.responses = [("hang", 2.0)]
    out = _provider(server).call("m1", MSGS, max_tokens=10, timeout=0.3)
    assert out.error == "timeout"
    assert out.seconds >= 0.3


def test_unreachable_endpoint_reports_unreachable(monkeypatch):
    _fast_retries(monkeypatch)
    p = make_provider(ProviderConfig(name="x", type="ollama",
                                     base_url="http://127.0.0.1:1/v1"))
    out = p.call("m1", MSGS, max_tokens=10)
    assert out.error is not None and "unreachable" in out.error


def test_malformed_json_is_an_error_not_a_crash(server):
    server.responses = [(200, b"this is not json {")]
    out = _provider(server).call("m1", MSGS, max_tokens=10)
    assert out.error is not None and "malformed" in out.error


def test_empty_content_is_success_with_empty_string(server):
    """A 200 with no content is a fact for classify_seat to judge, not a transport error."""
    server.responses = [(200, _ok_body("", reasoning="burned it all", finish="length"))]
    out = _provider(server).call("m1", MSGS, max_tokens=10)
    assert out.error is None
    assert out.content == ""
    assert out.reasoning == "burned it all"


def test_two_hundred_error_envelope_is_an_error_not_a_dead_seat(server):
    """Measured 2026-09-08 against the real OpenRouter endpoint: 2 of 6 identical probe
    calls to the starter panel's third seat came back HTTP 200 with an error envelope and
    no `choices` at all. Read as empty content, that is indistinguishable from a model
    burning its budget — the operator hunts the model instead of the route, and the
    upstream's own explanation is thrown away.
    """
    server.responses = [(200, json.dumps(
        {"error": {"code": 502, "message": "Provider returned error"}}).encode())]
    out = _provider(server).call("m1", MSGS, max_tokens=10)
    assert out.error is not None, "a 200 error envelope must not read as a dead seat"
    assert "Provider returned error" in out.error
    assert out.content == ""


def test_two_hundred_error_envelope_is_secret_redacted(server):
    """Same inbound-redaction rule as finding E: an error body that echoes request
    context must not put a credential in a seat report."""
    leak = "sk-or-v1-" + "a" * 40
    server.responses = [(200, json.dumps(
        {"error": {"message": f"upstream rejected key {leak}"}}).encode())]
    out = _provider(server).call("m1", MSGS, max_tokens=10)
    assert leak not in (out.error or "")
    assert "[REDACTED]" in (out.error or "")


def test_error_envelope_alongside_choices_is_still_a_report(server):
    """A gateway that sends a warning envelope AND a real completion must not lose the
    completion. Only a missing `choices` is the failure."""
    body = json.loads(_ok_body("the report").decode())
    body["error"] = None
    server.responses = [(200, json.dumps(body).encode())]
    out = _provider(server).call("m1", MSGS, max_tokens=10)
    assert out.error is None and out.content == "the report"


def test_openrouter_type_adds_attribution_header(server):
    server.responses = [(200, _ok_body("x"))]
    p = _provider(server, type_="openrouter")
    assert isinstance(p, OpenRouterProvider)
    p.call("m1", MSGS, max_tokens=10)
    assert server.requests[0][1].get("X-Title") == "agent-ops"


def test_list_models_returns_none_when_unreachable():
    p = make_provider(ProviderConfig(name="x", type="ollama",
                                     base_url="http://127.0.0.1:1/v1"))
    assert p.list_models(timeout=0.2) is None, "could-not-ask must be None, never empty"


def test_model_ids_and_context_lengths(server):
    listing = json.dumps({"data": [
        {"id": "big", "context_length": 200_000},
        {"id": "meta-only", "top_provider": {"context_length": 64_000}},
        {"id": "unknown-ctx"},
    ]}).encode()
    server.responses = [(200, listing), (200, listing)]
    p = _provider(server)
    assert p.model_ids() == {"big", "meta-only", "unknown-ctx"}
    ctx = p.context_lengths()
    assert ctx == {"big": 200_000, "meta-only": 64_000}, "unknown ctx stays absent, not 0"


def test_seat_output_defaults():
    o = SeatOutput()
    assert (o.content, o.error) == ("", None)


def test_inbound_error_text_is_secret_redacted(server):
    """Dogfood finding E, 2026-09-01: outbound payloads are secret-gated but error bodies
    coming BACK were written to reports and stderr verbatim — an endpoint echoing request
    context into its error would put a live credential on disk."""
    leaked = "bad key: " + "ghp_" + "a" * 24 + " rejected"
    server.responses = [(401, json.dumps({"error": {"message": leaked}}).encode())]
    out = _provider(server).call("m", MSGS, max_tokens=10)
    assert out.error is not None and "HTTP 401" in out.error
    assert "ghp_" not in out.error
    assert "[REDACTED]" in out.error
    assert "rejected" in out.error, "the non-secret part of the message must survive"


def _provider_with_headers(script, headers, *, key_env=None, type_="openai-compatible"):
    return make_provider(ProviderConfig(name="test", type=type_, base_url=script.base_url,
                                        api_key_env=key_env, headers=headers))


def test_config_headers_are_sent_on_the_wire(server):
    """Dogfood finding C: no way to tag spend per run at the provider. Headers from the
    provider config must reach the actual request."""
    server.responses = [(200, _ok_body("ok"))]
    p = _provider_with_headers(server, {"X-Cost-Center": "platform-eng"})
    p.call("m", MSGS, max_tokens=10)
    _, headers, _ = server.requests[0]
    assert headers.get("X-Cost-Center") == "platform-eng"


def test_config_headers_cannot_displace_authorization(server, monkeypatch):
    """Authorization is applied AFTER config headers — belt to the config loader's braces
    (which refuses an authorization key outright)."""
    monkeypatch.setenv("THE_KEY", "real-key")
    server.responses = [(200, _ok_body("ok"))]
    # Bypass load_config's refusal on purpose to pin the layer's own ordering.
    p = _provider_with_headers(server, {"Authorization": "Bearer forged"},
                               key_env="THE_KEY")
    p.call("m", MSGS, max_tokens=10)
    _, headers, _ = server.requests[0]
    assert headers.get("Authorization") == "Bearer real-key"


def test_config_headers_can_override_openrouter_attribution(server):
    server.responses = [(200, _ok_body("ok"))]
    p = _provider_with_headers(server, {"X-Title": "my-org-reviews"}, type_="openrouter")
    p.call("m", MSGS, max_tokens=10)
    _, headers, _ = server.requests[0]
    assert headers.get("X-Title") == "my-org-reviews"


# ── Ollama ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,listed,expected", [
    # Ollama lists every tag spelled out but serves a bare name as :latest — measured
    # against a live daemon 2026-09-30 (embeddings on `nomic-embed-text` -> 200 while
    # /v1/models lists only `nomic-embed-text:latest`). The starter panel from
    # `init --ollama` uses bare names, so an exact-match preflight rejected all of them.
    ("llama3.1", {"llama3.1:latest"}, True),
    ("llama3.1:latest", {"llama3.1:latest"}, True),
    ("llama3.1:8b", {"llama3.1:8b"}, True),
    # Only :latest is implied. A bare name must not match some other pulled tag, and an
    # explicit tag must not fall back to :latest — that would report a model Ollama
    # would answer with a 404 as routable.
    ("llama3.1", {"llama3.1:8b"}, False),
    ("llama3.1:70b", {"llama3.1:latest"}, False),
    ("llama3.1", set(), False),
])
def test_ollama_preflight_understands_the_implicit_latest_tag(model, listed, expected):
    p = make_provider(ProviderConfig(name="o", type="ollama", base_url="http://127.0.0.1:1/v1"))
    assert p.lists_model(model, listed) is expected


def test_other_providers_still_require_an_exact_id():
    p = make_provider(ProviderConfig(name="g", type="openai-compatible",
                                     base_url="http://127.0.0.1:1/v1"))
    assert p.lists_model("llama3.1", {"llama3.1:latest"}) is False


def test_ollama_hosted_api_authenticates_with_the_key_from_env(server, monkeypatch):
    """The hosted API is the same provider type as the local daemon plus a key."""
    monkeypatch.setenv("TEST_OLLAMA_KEY", "hosted-key-value")
    server.responses = [(200, _ok_body("ok"))]
    _provider(server, key_env="TEST_OLLAMA_KEY", type_="ollama").call("m", MSGS, max_tokens=5)
    path, headers, _ = server.requests[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer hosted-key-value"


# ── response triage ────────────────────────────────────────────────────────────────────

def test_a_non_object_json_body_is_a_seat_error_not_a_crash(server):
    """A 200 whose JSON is not an object used to raise out of call() — the error-envelope
    check ran outside the shape guard — taking the whole panel down with one seat."""
    server.responses = [(200, b"[]")]
    out = _provider(server).call("m", MSGS, max_tokens=10)
    assert out.error is not None and out.error.startswith("malformed response shape")


# ── custom providers: `type = "module:ClassName"` ──────────────────────────────────────

import importlib
import sys
import textwrap

MESSAGES_DIALECT = textwrap.dedent('''
    from agent_ops.providers import BaseProvider

    class MessagesProvider(BaseProvider):
        """A different dialect: another path, x-api-key auth, system prompt lifted out,
        content blocks instead of choices."""
        chat_path = "/messages"

        def _headers(self):
            h = super()._headers()
            h.pop("Authorization", None)
            if self.api_key:
                h["x-api-key"] = self.api_key
            return h

        def build_body(self, model, messages, *, max_tokens, temperature):
            body = {"model": model, "max_tokens": max_tokens,
                    "messages": [m for m in messages if m["role"] != "system"]}
            system = [m["content"] for m in messages if m["role"] == "system"]
            if system:
                body["system"] = "\\n".join(system)
            return body

        def parse_response(self, data):
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            return {"content": text, "finish_reason": data.get("stop_reason") or ""}
''')


@pytest.fixture()
def drop_in(tmp_path, monkeypatch):
    """A scratch AGENT_OPS_HOME with an empty providers/ dir; sys.path and the module
    cache are restored afterwards so one test's provider cannot leak into the next."""
    monkeypatch.setenv("AGENT_OPS_HOME", str(tmp_path))
    d = tmp_path / "providers"
    d.mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    yield d
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


def _module(drop_in, name, source):
    (drop_in / f"{name}.py").write_text(source, encoding="utf-8")
    importlib.invalidate_caches()


def _custom(type_, base_url="http://127.0.0.1:1/v1", **kw):
    return make_provider(ProviderConfig(name="custom", type=type_, base_url=base_url, **kw))


def test_a_dropped_in_provider_speaks_another_dialect_end_to_end(server, drop_in, monkeypatch):
    monkeypatch.setenv("TEST_MESSAGES_KEY", "messages-key")
    _module(drop_in, "mydialect", MESSAGES_DIALECT)
    server.responses = [(200, json.dumps({"content": [{"type": "text", "text": "the report"}],
                                          "stop_reason": "end_turn"}).encode())]
    p = _custom("mydialect:MessagesProvider", server.base_url, api_key_env="TEST_MESSAGES_KEY")
    out = p.call("m1", [{"role": "system", "content": "be strict"},
                        {"role": "user", "content": "hi"}], max_tokens=50)
    assert out.error is None
    assert (out.content, out.finish_reason) == ("the report", "end_turn")
    path, headers, body = server.requests[0]
    assert path == "/v1/messages"
    sent = {k.lower(): v for k, v in headers.items()}      # urllib title-cases on the wire
    assert sent["x-api-key"] == "messages-key" and "authorization" not in sent
    assert body["system"] == "be strict" and body["messages"] == [{"role": "user", "content": "hi"}]
    assert "temperature" not in body


def test_seat_params_reach_a_custom_dialect_body_but_cannot_override_what_it_built(
        server, drop_in):
    """Seat `params` are merged in call(), under whatever build_body returned, so they
    work in every dialect — and a params key naming a field the dialect owns (here the
    lifted-out `system`, and the model) loses to the dialect's own body."""
    _module(drop_in, "mydialect", MESSAGES_DIALECT)
    server.responses = [(200, json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode())]
    p = _custom("mydialect:MessagesProvider", server.base_url)
    out = p.call("m1", [{"role": "system", "content": "be strict"},
                        {"role": "user", "content": "hi"}], max_tokens=50,
                 params={"top_k": 5, "system": "params must not win", "model": "other"})
    assert out.error is None
    body = server.requests[0][2]
    assert body["top_k"] == 5, "a params field the dialect does not own must reach the wire"
    assert body["system"] == "be strict" and body["model"] == "m1"


def test_a_custom_provider_inherits_the_retry_and_triage_of_the_base(server, drop_in, monkeypatch):
    _fast_retries(monkeypatch)
    _module(drop_in, "mydialect", MESSAGES_DIALECT)
    server.responses = [(500, b"boom"),
                        (200, json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode())]
    out = _custom("mydialect:MessagesProvider", server.base_url).call("m", MSGS, max_tokens=5)
    assert out.error is None and out.content == "ok"
    assert len(server.requests) == 2


def test_a_dialect_body_of_the_wrong_shape_is_a_seat_error_not_a_crash(server, drop_in):
    """`data["content"]` is what every author writes first, and it raises KeyError on a
    body without it. That must land in SeatOutput.error like any other bad reply; if it
    escaped, one custom seat would take the whole panel down."""
    _module(drop_in, "mydialect", MESSAGES_DIALECT)
    server.responses = [(200, b'{"unexpected": true}')]
    out = _custom("mydialect:MessagesProvider", server.base_url).call("m", MSGS, max_tokens=5)
    assert out.error == "malformed response shape: KeyError"


def test_the_drop_in_dir_is_appended_so_it_cannot_shadow_anything(drop_in):
    _module(drop_in, "mydialect", MESSAGES_DIALECT)
    _custom("mydialect:MessagesProvider")
    assert sys.path[-1] == str(drop_in), "prepending would let a stray file shadow the stdlib"


def test_a_missing_module_names_where_it_was_looked_for(drop_in):
    with pytest.raises(ConfigError) as e:
        _custom("no_such_provider_module:Thing")
    msg = str(e.value)
    assert "no module 'no_such_provider_module'" in msg and str(drop_in) in msg


def test_a_provider_missing_its_own_dependency_is_not_blamed_on_the_path(drop_in):
    _module(drop_in, "needs_dep", "import absent_dependency_xyz\n")
    with pytest.raises(ConfigError) as e:
        _custom("needs_dep:P")
    assert "absent_dependency_xyz" in str(e.value)
    assert "put needs_dep.py" not in str(e.value), "the module WAS found; its import failed"


def test_an_exception_at_import_is_a_config_error_naming_the_cause(drop_in):
    _module(drop_in, "boom_at_import", "raise RuntimeError('kaput')\n")
    with pytest.raises(ConfigError, match="RuntimeError: kaput"):
        _custom("boom_at_import:P")


@pytest.mark.parametrize("source,attr", [
    (MESSAGES_DIALECT, "Missing"),                                # no such attribute
    ("class Plain:\n    pass\n", "Plain"),                        # not a BaseProvider
    ("def make(cfg):\n    return None\n", "make"),                # a factory is not a class
])
def test_the_named_object_must_be_a_baseprovider_subclass(drop_in, source, attr):
    _module(drop_in, "notaprovider", source)
    with pytest.raises(ConfigError, match="must be a class deriving from"):
        _custom(f"notaprovider:{attr}")


@pytest.mark.parametrize("bad", ["mod:", ":Cls", "mod:a:b", "mod:1x", "mod:with space",
                                 "../../evil:P", ".rel:P", "a..b:P", "a/b:P", "pkg.:P"])
def test_a_malformed_custom_type_says_the_expected_shape(drop_in, bad):
    with pytest.raises(ConfigError, match="module:ClassName"):
        _custom(bad)


def test_a_provider_constructor_failure_is_a_config_error_but_config_errors_pass_through(
        drop_in, monkeypatch):
    monkeypatch.delenv("TEST_ABSENT_KEY", raising=False)
    _module(drop_in, "flaky", textwrap.dedent('''
        from agent_ops.providers import BaseProvider
        class Explodes(BaseProvider):
            def __init__(self, cfg):
                raise ValueError("no region configured")
        class Fine(BaseProvider):
            pass
    '''))
    with pytest.raises(ConfigError, match="failed to initialise: ValueError: no region"):
        _custom("flaky:Explodes")
    # The base class's own missing-key ConfigError must reach the user unchanged, not be
    # re-wrapped as "failed to initialise".
    with pytest.raises(ConfigError) as e:
        _custom("flaky:Fine", api_key_env="TEST_ABSENT_KEY")
    assert "TEST_ABSENT_KEY" in str(e.value) and "failed to initialise" not in str(e.value)


def test_the_unknown_type_error_points_at_custom_providers():
    with pytest.raises(ConfigError, match="module:ClassName"):
        make_provider(ProviderConfig(name="x", type="carrier-pigeon",
                                     base_url="http://127.0.0.1:1/v1"))


# --- per-seat params (body fields) -----------------------------------------------------------

def test_seat_params_are_merged_into_the_wire_body(server):
    """The OpenRouter route pin must reach the actual JSON the endpoint receives."""
    server.responses = [(200, _ok_body("ok"))]
    _provider(server, type_="openrouter").call(
        "m1", MSGS, max_tokens=100, params={"provider": {"only": ["Venice"]}})
    _, _, body = server.requests[0]
    assert body == {"model": "m1", "messages": MSGS, "max_tokens": 100,
                    "provider": {"only": ["Venice"]}}


def test_seat_params_cannot_override_client_owned_fields(server, monkeypatch):
    """Belt to load_config's braces: even a params dict that skipped validation cannot
    change the model, messages or budget, cannot turn on streaming, and never reaches
    the headers."""
    monkeypatch.setenv("THE_KEY", "real-key")
    server.responses = [(200, _ok_body("ok"))]
    forged = {"model": "evil", "messages": [], "max_tokens": 999999,
              "max_completion_tokens": 999999, "stream": True,
              "Authorization": "Bearer forged", "headers": {"Authorization": "Bearer forged"},
              "provider": {"only": ["Venice"]}}
    _provider(server, key_env="THE_KEY").call("m1", MSGS, max_tokens=100, params=forged)
    _, headers, body = server.requests[0]
    assert body["model"] == "m1"
    assert body["messages"] == MSGS
    assert body["max_tokens"] == 100
    assert "max_completion_tokens" not in body and "stream" not in body
    assert body["provider"] == {"only": ["Venice"]}
    assert headers.get("Authorization") == "Bearer real-key"


def test_explicit_temperature_beats_seat_params(server):
    """probe runs every seat at temperature=1.0; a seat's params must not skew its score."""
    server.responses = [(200, _ok_body("ok"))]
    _provider(server).call("m1", MSGS, max_tokens=10, temperature=1.0,
                           params={"temperature": 0.0})
    assert server.requests[0][2]["temperature"] == 1.0


def test_seat_params_from_config_reach_the_wire(server, tmp_path):
    """End to end: panel.toml -> Seat.params -> run_seat -> HTTP body."""
    from agent_ops.config import load_config
    from agent_ops.main import run_seat
    cfg_path = tmp_path / "panel.toml"
    cfg_path.write_text(f"""
[[seats]]
name = "s"
family = "f"
provider = "p"
model = "m1"
params = {{ provider = {{ only = ["Venice"] }} }}

[providers.p]
type = "openrouter"
base_url = "{server.base_url}"
""", encoding="utf-8")
    cfg = load_config(cfg_path)
    server.responses = [(200, _ok_body("AUDIT COMPLETE - 0 findings"))]
    run_seat(make_provider(cfg.providers["p"]), cfg.seats[0], "prompt", tmp_path,
             timeout=5, max_tokens=42)
    _, _, body = server.requests[0]
    assert body["provider"] == {"only": ["Venice"]}
    assert body["model"] == "m1" and body["max_tokens"] == 42


# ── confirm_unlisted: providers that list only part of what they serve ─────────────────

@pytest.mark.parametrize("status,expected", [(200, True), (404, False), (500, False)])
def test_ollama_confirms_an_unlisted_model_by_asking_api_show(server, status, expected):
    """A local daemon lists only what was `ollama pull`ed but serves every cloud model on
    demand (glm-5.3-flash:cloud, unlisted, answered a chat call on daemon 0.31.2). /api/show
    is 200 for those and 404 for an id that does not exist; anything else is unconfirmed."""
    server.responses = [(status, b'{"details": {}}' if status == 200 else b'{"error": "no"}')]
    assert _provider(server, type_="ollama").confirm_unlisted("glm-5.3-flash:cloud") is expected
    path, _, body = server.requests[0]
    assert path == "/api/show", "the probe lives at the API root, not under /v1"
    assert body == {"model": "glm-5.3-flash:cloud"}


def test_ollama_confirm_carries_the_key_and_an_unreachable_daemon_is_unconfirmed(
        server, monkeypatch):
    monkeypatch.setenv("TEST_OLLAMA_KEY", "hosted-key")
    server.responses = [(200, b"{}")]
    assert _provider(server, type_="ollama", key_env="TEST_OLLAMA_KEY").confirm_unlisted("m")
    assert server.requests[0][1]["Authorization"] == "Bearer hosted-key"
    dead = make_provider(ProviderConfig(name="o", type="ollama", base_url="http://127.0.0.1:1/v1"))
    assert dead.confirm_unlisted("m", timeout=0.2) is False


def test_a_listing_is_authoritative_unless_a_provider_overrides_confirm_unlisted(server):
    for type_ in ("openai-compatible", "openrouter"):
        assert _provider(server, type_=type_).confirm_unlisted("anything") is False
    assert server.requests == [], "the default must never touch the network"
