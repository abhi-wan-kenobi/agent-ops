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


def test_ollama_cloud_flavor_wants_a_key_and_points_at_the_hosted_api(tmp_path, capsys):
    target = tmp_path / "panel.toml"
    assert run_init(["--ollama-cloud", "--config", str(target)]) == 0
    cfg = load_config(target)
    assert len({s.family for s in cfg.seats}) >= 3, "coder-family exclusion needs spare families"
    prov = cfg.providers["ollama-cloud"]
    assert (prov.type, prov.base_url, prov.api_key_env) == (
        "ollama", "https://ollama.com/v1", "OLLAMA_API_KEY")
    assert {s.provider for s in cfg.seats} == {"ollama-cloud"}
    out = capsys.readouterr().out
    assert "export OLLAMA_API_KEY" in out and "agent_ops probe" in out
    assert "no API key needed" not in out


def test_init_never_overwrites(tmp_path, capsys):
    target = tmp_path / "panel.toml"
    target.write_text("# my hand-curated panel\n", encoding="utf-8")
    assert run_init(["--config", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "# my hand-curated panel\n"
    assert "never overwrites" in capsys.readouterr().err


def test_flavors_are_mutually_exclusive(tmp_path):
    for pair in (["--openrouter", "--ollama"], ["--ollama", "--ollama-cloud"],
                 ["--openrouter", "--ollama-cloud"]):
        with pytest.raises(SystemExit):
            run_init([*pair, "--config", str(tmp_path / "p.toml")])


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


def test_unwritable_target_is_a_clean_error_not_a_traceback(tmp_path, capsys):
    """Panel finding, 2026-09-01 (three seats across two lanes): uncaught OSError."""
    import os
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        rc = run_init(["--config", str(ro / "sub" / "panel.toml")])
    finally:
        os.chmod(ro, 0o700)
    assert rc == 1
    assert "ERROR: cannot write" in capsys.readouterr().err


def test_init_follows_agent_ops_home(tmp_path, monkeypatch, capsys):
    """init with no --config must write under AGENT_OPS_HOME, like every other subcommand.

    v0.3.0 shipped it reading the frozen DEFAULT_CONFIG_PATH literal instead, so `init`
    wrote to ~/.agent-ops while `audit` and `probe` read the scratch home — on a machine
    that already had a panel it refused outright, and on a clean one it wrote a config the
    rest of the tool could not see. Found in the clean-profile install rerun, 2026-09-08.
    """
    scratch = tmp_path / "home"
    monkeypatch.setenv("AGENT_OPS_HOME", str(scratch))
    assert run_init([]) == 0
    written = scratch / "panel.toml"
    assert written.is_file(), "init ignored AGENT_OPS_HOME"
    assert load_config(written).seats
    # And the next-steps block must not tell the user to pass --config for a default path.
    assert "--config" not in capsys.readouterr().out


def test_init_starter_panel_matches_panel_example():
    """init's embedded panel and panel.example.toml are two copies of one decision.

    They already drifted once (the dated verification comment), and a drift in the MODEL
    IDS would hand new users a panel that was never probed. The comment in both files says
    'keep the two in sync'; this is what makes that true.
    """
    import pathlib
    import tomllib

    from agent_ops.init_cmd import OPENROUTER_TOML

    example = pathlib.Path(__file__).resolve().parents[1] / "panel.example.toml"
    theirs = tomllib.loads(example.read_text(encoding="utf-8"))["seats"]
    ours = tomllib.loads(OPENROUTER_TOML)["seats"]
    key = lambda s: (s["family"], s["provider"], s["model"])   # noqa: E731
    assert [key(s) for s in ours] == [key(s) for s in theirs]
