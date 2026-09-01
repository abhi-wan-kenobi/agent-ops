"""Shared config loader for the agent-ops hooks.

Two layers: `~/.agent-ops/hooks.toml` (user-wide) overlaid by
`<project>/.agent-ops/hooks.toml` (project wins, per key). The project directory comes
from CLAUDE_PROJECT_DIR when the harness sets it, else the working directory.

FAIL OPEN, everywhere. These hooks are guard rails, not a security boundary: a malformed
config, an unreadable file, or a parser bug must degrade to "no extra blocking", never to
"no edits possible" — a hook that bricks the session teaches people to delete it.

With NO config at all both hooks still provide value: the dangerous-git block list
defaults on, and tag protection is active under the default tag names. Zero-config safety.
"""
from __future__ import annotations

import os
import pathlib
import tomllib
from typing import Any

USER_CONFIG = "~/.agent-ops/hooks.toml"
PROJECT_CONFIG_RELPATH = ".agent-ops/hooks.toml"

DEFAULTS: dict[str, Any] = {
    "protection": {
        "readonly_roots": [],
        "tags": {
            "readonly": "claude-readonly",
            "ignore": "claude-ignore",
            "draft": "claude-draft",
        },
    },
    "dangerous_git": {
        "always_block": [
            "reset --hard",
            "clean -f",
            "branch -D",
            "checkout .",
            "restore .",
            "push --force",
            "push -f",
            "push --force-with-lease",
        ],
        "shared_worktrees": [],
    },
}


def _read_toml(path: pathlib.Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


def load_hooks_config(project_dir: str | os.PathLike | None = None) -> dict[str, Any]:
    """Defaults <- user file <- project file, key by key within each section."""
    merged: dict[str, Any] = {
        section: dict(table) for section, table in DEFAULTS.items()
    }
    merged["protection"]["tags"] = dict(DEFAULTS["protection"]["tags"])

    project = pathlib.Path(project_dir or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    layers = [
        pathlib.Path(USER_CONFIG).expanduser(),
        project / PROJECT_CONFIG_RELPATH,
    ]
    for layer in layers:
        if not layer.is_file():
            continue
        raw = _read_toml(layer)
        for section, table in raw.items():
            if not isinstance(table, dict):
                continue
            dst = merged.setdefault(section, {})
            for key, value in table.items():
                if key == "tags" and isinstance(value, dict):
                    # Tag names merge per tag, so a project can rename one tag without
                    # silently disabling the other two.
                    dst.setdefault("tags", {}).update(
                        {k: v for k, v in value.items() if isinstance(v, str)})
                else:
                    dst[key] = value
    return merged
