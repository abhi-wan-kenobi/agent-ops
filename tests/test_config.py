"""panel.toml loading and validation — the seam every other module trusts."""
from __future__ import annotations

import pathlib
import textwrap

import pytest

from agent_ops import config as cfg_mod
from agent_ops.config import ConfigError, load_config

GOOD = """
[agent_ops]
outroot = "~/somewhere/audits"
state_dir = "~/somewhere/state"
max_payload = 123456

[[seats]]
name = "seat-a"
family = "deepseek"
provider = "openrouter"
model = "deepseek/deepseek-chat"

[[seats]]
name = "local"
family = "llama"
provider = "ollama"
model = "llama3.1"

[providers.openrouter]
type = "openrouter"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"

[providers.ollama]
type = "ollama"
base_url = "http://localhost:11434/v1/"
"""


def _write(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    p = tmp_path / "panel.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_good_config_parses_with_expansion_and_defaults(tmp_path):
    c = load_config(_write(tmp_path, GOOD))
    assert c.outroot == pathlib.Path("~/somewhere/audits").expanduser()
    assert c.state_dir == pathlib.Path("~/somewhere/state").expanduser()
    assert c.max_payload == 123456
    assert c.max_tokens == cfg_mod.DEFAULT_MAX_TOKENS, "unset keys must default"
    assert [s.name for s in c.seats] == ["seat-a", "local"]
    assert c.seats[0].model == "deepseek/deepseek-chat"
    assert c.providers["openrouter"].api_key_env == "OPENROUTER_API_KEY"
    assert c.providers["ollama"].api_key_env is None, "omitted key env means no auth"
    assert c.providers["ollama"].base_url == "http://localhost:11434/v1", (
        "trailing slash must be normalized or URLs double up")
    # Derived state paths hang off state_dir.
    assert c.runs_dir == c.state_dir / "runs"
    assert c.roster_path == c.state_dir / "roster.json"


def test_missing_explicit_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_missing_default_config_yields_seatless_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_PATH", str(tmp_path / "absent.toml"))
    c = load_config()
    assert c.seats == [] and c.providers == {}
    assert c.max_payload == cfg_mod.DEFAULT_MAX_PAYLOAD


def test_seat_missing_model_is_an_error(tmp_path):
    body = GOOD.replace('model = "deepseek/deepseek-chat"\n', "")
    with pytest.raises(ConfigError, match="model"):
        load_config(_write(tmp_path, body))


def test_provider_missing_type_is_an_error(tmp_path):
    body = GOOD.replace('type = "openrouter"\n', "")
    with pytest.raises(ConfigError, match="type"):
        load_config(_write(tmp_path, body))


def test_seat_referencing_undefined_provider_is_an_error(tmp_path):
    body = GOOD.replace('provider = "ollama"', 'provider = "does-not-exist"')
    with pytest.raises(ConfigError, match="does-not-exist"):
        load_config(_write(tmp_path, body))


def test_duplicate_seat_names_are_an_error(tmp_path):
    body = GOOD.replace('name = "local"', 'name = "seat-a"')
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(_write(tmp_path, body))


def test_invalid_toml_is_a_config_error_not_a_traceback(tmp_path):
    with pytest.raises(ConfigError, match="TOML"):
        load_config(_write(tmp_path, "[agent_ops\noops"))


def test_nonpositive_max_payload_is_rejected(tmp_path):
    body = GOOD.replace("max_payload = 123456", "max_payload = 0")
    with pytest.raises(ConfigError, match="max_payload"):
        load_config(_write(tmp_path, body))


def test_family_is_normalized_to_lowercase(tmp_path):
    body = GOOD.replace('family = "deepseek"', 'family = "DeepSeek"')
    c = load_config(_write(tmp_path, body))
    assert c.seats[0].family == "deepseek"


def test_relative_state_paths_are_anchored_at_load_time(tmp_path, monkeypatch):
    """Audit finding: a relative outroot/state_dir would drift with any later chdir."""
    monkeypatch.chdir(tmp_path)
    body = GOOD.replace('outroot = "~/somewhere/audits"', 'outroot = "rel/audits"')
    c = load_config(_write(tmp_path, body))
    assert c.outroot.is_absolute()
    assert c.outroot == tmp_path / "rel" / "audits"


def test_non_http_base_url_is_rejected_at_load(tmp_path):
    """Rejected as a security finding (the config is the user's own file) but kept as
    validation: a schemeless/typo base_url should fail here with a nameable message, not
    later as one urllib error per seat."""
    body = GOOD.replace('base_url = "https://openrouter.ai/api/v1"',
                        'base_url = "openrouter.ai/api/v1"')
    with pytest.raises(ConfigError, match="http"):
        load_config(_write(tmp_path, body))


# --- provider headers (v0.2.2, dogfood finding C) --------------------------------------------

def _headers_toml(tmp_path, headers_block):
    p = tmp_path / "panel.toml"
    p.write_text(f"""
[[seats]]
name = "s"
family = "f"
provider = "p"
model = "m"

[providers.p]
type = "openai-compatible"
base_url = "http://x/v1"
{headers_block}
""", encoding="utf-8")
    return p


def test_provider_headers_parse_and_default_empty(tmp_path):
    cfg = load_config(_headers_toml(tmp_path, '[providers.p.headers]\n"X-Team" = "eng"'))
    assert cfg.providers["p"].headers == {"X-Team": "eng"}
    cfg2 = load_config(_headers_toml(tmp_path, ""))     # same path, headerless rewrite
    assert cfg2.providers["p"].headers == {}


def test_authorization_header_in_config_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="api_key_env"):
        load_config(_headers_toml(
            tmp_path, '[providers.p.headers]\nauthorization = "Bearer sk-x"'))


def test_content_type_header_in_config_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="Content-Type"):
        load_config(_headers_toml(
            tmp_path, '[providers.p.headers]\n"content-type" = "text/plain"'))


def test_non_string_header_value_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="must be a string"):
        load_config(_headers_toml(tmp_path, '[providers.p.headers]\n"X-N" = 5'))
