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


def test_generic_and_ollama_types_are_the_same_implementation(server):
    assert type(_provider(server, type_="ollama")) is OpenAICompatProvider


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
