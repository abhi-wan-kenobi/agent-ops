"""`agent_ops init` — write a starter panel.toml and say exactly what to do next.

First-run friction is an adoption killer measured directly: v0.1's setup was "copy a file
out of the plugin directory, find that directory first". This command removes the
scavenger hunt: one command writes a working config, and its output IS the quickstart —
the key-export line and the probe command, in order.

It never overwrites. A panel.toml is user-owned and hand-edited; clobbering one to
"refresh" it would destroy a curated seat list. Re-initialising is expressed by deleting
the file first, deliberately, by hand.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config

# Model ids verified against openrouter.ai/api/v1/models on 2026-09-01 (same starter panel
# as panel.example.toml — keep the two in sync when either changes).
OPENROUTER_TOML = """\
# agent-ops panel — written by `agent_ops init`. Edit freely; this file is yours.
#
# Seats are the reviewers. Keep them in DIFFERENT model families: the panel mechanically
# excludes the family that wrote the code (--coder), and with one family that exclusion
# would leave nothing. Run `agent_ops probe` after editing: it scores every seat on a
# known-defect diff and ranks the usable ones into roster.json.

# ── Starter panel: three cheap, diverse OpenRouter families ────────────────────────────
# Typical review cost (a few thousand chars of diff, three seats): well under US$0.05.

[[seats]]
name = "seat-a"
family = "minimax"
provider = "openrouter"
model = "minimax/minimax-m2.7"

[[seats]]
name = "seat-b"
family = "gpt"
provider = "openrouter"
model = "openai/gpt-oss-120b"

[[seats]]
name = "seat-c"
family = "glm"
provider = "openrouter"
model = "z-ai/glm-5.3-flash"

# ── Free path: a local Ollama model, no API key at all ─────────────────────────────────
# [[seats]]
# name = "local"
# family = "llama"
# provider = "ollama"
# model = "llama3.1"

[providers.openrouter]
type = "openrouter"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"

# [providers.ollama]
# type = "ollama"
# base_url = "http://localhost:11434/v1"
"""

# The Ollama flavor cannot know which models are pulled locally, so it starts from three
# widely-pulled families and says out loud that the list must match `ollama list`.
OLLAMA_TOML = """\
# agent-ops panel — written by `agent_ops init --ollama`. Edit freely; this file is yours.
#
# ⚠️ EDIT THE MODELS FIRST: these must name models you have actually pulled — check with
# `ollama list` and pull what's missing (`ollama pull llama3.1` etc). Keep seats in
# DIFFERENT model families: the panel mechanically excludes the family that wrote the code
# (--coder), and with one family that exclusion would leave nothing. Run `agent_ops probe`
# after editing: it scores every seat on a known-defect diff and ranks the usable ones.

[[seats]]
name = "llama"
family = "llama"
provider = "ollama"
model = "llama3.1"

[[seats]]
name = "qwen"
family = "qwen"
provider = "ollama"
model = "qwen3"

[[seats]]
name = "mistral"
family = "mistral"
provider = "ollama"
model = "mistral"

[providers.ollama]
type = "ollama"
base_url = "http://localhost:11434/v1"
# no api_key_env: a local daemon needs no auth

# ── Optional: add a cloud family alongside the local ones ──────────────────────────────
# [providers.openrouter]
# type = "openrouter"
# base_url = "https://openrouter.ai/api/v1"
# api_key_env = "OPENROUTER_API_KEY"
"""


def run_init(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="agent_ops init",
        description="Write a starter panel.toml (never overwrites an existing one)")
    flavor = ap.add_mutually_exclusive_group()
    flavor.add_argument("--openrouter", action="store_true",
                        help="OpenRouter starter panel (default): one key, three families")
    flavor.add_argument("--ollama", action="store_true",
                        help="local-Ollama starter panel: no key at all")
    ap.add_argument("--config", help=f"where to write it (default {DEFAULT_CONFIG_PATH})")
    a = ap.parse_args(argv)

    target = pathlib.Path(a.config or DEFAULT_CONFIG_PATH).expanduser()
    if target.exists():
        print(f"REFUSED: {target} already exists — init never overwrites a panel config.\n"
              f"         Edit it directly, or pass --config to write somewhere else.",
              file=sys.stderr)
        return 1

    text = OLLAMA_TOML if a.ollama else OPENROUTER_TOML
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

    # The file just written must survive the loader's own validation — shipping an init
    # that produces a config `audit` then rejects would be worse than no init at all.
    try:
        load_config(target)
    except ConfigError as e:                                    # pragma: no cover
        print(f"BUG: init wrote a config its own loader rejects ({e}) — please report",
              file=sys.stderr)
        return 2

    cfg_flag = f" --config {target}" if a.config else ""
    print(f"wrote {target}")
    print()
    if a.ollama:
        print("Next steps (no API key needed — seats run on your local Ollama daemon):")
        print(f"  1. edit {target} so the models match `ollama list`")
        print(f"  2. python3 -m agent_ops probe{cfg_flag}     # score & rank the seats")
        print(f"  3. python3 -m agent_ops <repo> --coder <model that wrote the code>"
              f"{cfg_flag}")
    else:
        print("Next steps:")
        print("  1. export OPENROUTER_API_KEY=sk-or-...       # get one at openrouter.ai/keys")
        print(f"  2. python3 -m agent_ops probe{cfg_flag}     # score & rank the seats")
        print(f"  3. python3 -m agent_ops <repo> --coder <model that wrote the code>"
              f"{cfg_flag}")
    return 0
