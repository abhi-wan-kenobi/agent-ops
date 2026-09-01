"""`agent_ops init` — the two-command path from nothing to a working panel config.

The exit criterion from the v0.2 plan: a fresh user goes key → reviewing in two commands.
That only holds if init's output parses under the real loader, carries enough family
diversity to survive --coder exclusion, and never eats an existing hand-edited config.
"""
from __future__ import annotations

import pytest

from agent_ops.config import load_config
from agent_ops.init_cmd import run_init
from agent_ops.main import main


def test_default_flavor_writes_a_loadable_openrouter_panel(tmp_path, capsys):
    target = tmp_path / "panel.toml"
    assert run_init(["--config", str(target)]) == 0
    cfg = load_config(target)
    # Three distinct families: two would collapse to one usable seat under --coder
    # exclusion the moment the coder matches either.
    assert len({s.family for s in cfg.seats}) >= 3
    assert all(s.provider == "openrouter" for s in cfg.seats)
    assert cfg.providers["openrouter"].api_key_env == "OPENROUTER_API_KEY"
    out = capsys.readouterr().out
    assert "export OPENROUTER_API_KEY" in out
    assert "agent_ops probe" in out


def test_ollama_flavor_needs_no_key_and_says_edit_models_first(tmp_path, capsys):
    target = tmp_path / "panel.toml"
    assert run_init(["--ollama", "--config", str(target)]) == 0
    cfg = load_config(target)
    assert len({s.family for s in cfg.seats}) >= 3
    assert cfg.providers["ollama"].api_key_env is None
    out = capsys.readouterr().out
    assert "no API key needed" in out
    assert "ollama list" in out


def test_init_never_overwrites(tmp_path, capsys):
    target = tmp_path / "panel.toml"
    target.write_text("# my hand-curated panel\n", encoding="utf-8")
    assert run_init(["--config", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "# my hand-curated panel\n"
    assert "never overwrites" in capsys.readouterr().err


def test_flavors_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        run_init(["--openrouter", "--ollama", "--config", str(tmp_path / "p.toml")])


def test_init_creates_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "panel.toml"
    assert run_init(["--config", str(target)]) == 0
    assert target.is_file()


def test_init_is_wired_as_a_subcommand(tmp_path, capsys):
    target = tmp_path / "panel.toml"
    assert main(["init", "--config", str(target)]) == 0
    assert target.is_file()
    # The printed follow-up commands must carry the non-default path, or a user who ran
    # init --config would copy-paste commands that read a config that does not exist.
    assert f"--config {target}" in capsys.readouterr().out
