#!/usr/bin/env python3
"""PreToolUse hook: block edits to protected files, independent of permission mode.

Three protections, all configurable via hooks.toml (see hook_config.DEFAULTS):

1. `readonly_roots` — no Edit/Write/MultiEdit/NotebookEdit anywhere under these paths.
2. Tag protection — a file whose text carries the readonly or ignore tag (outside code
   fences and inline code spans) must not be edited. The tags mark human-owned files; the
   fence exception exists so documentation ABOUT the tags stays editable.
3. Tag introduction — an edit must not ADD any bare tag: an agent granting itself (or a
   file) a protection status is exactly the kind of quiet self-dealing this hook exists to
   stop. Mention tags in code fences or inline code instead.

FAILS OPEN: any internal error exits 0 (allow). This is a guard rail, not a security
boundary — a hook that can brick every edit is worse than one that occasionally misses.

Exit 2 blocks the call; the stderr message is shown to the model.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_config  # noqa: E402

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def text_outside_code(text: str) -> str:
    """The document with fenced blocks and inline code spans removed — the only regions
    where a tag counts as LIVE rather than merely mentioned."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(INLINE_CODE_RE.sub("", line))
    return "\n".join(out)


def live_tags(text: str, tags: dict[str, str]) -> set[str]:
    visible = text_outside_code(text)
    return {name for name, literal in tags.items() if literal and literal in visible}


def new_content_of(tool_name: str, tool_input: dict) -> str:
    """Every piece of text this call would ADD to the file."""
    parts: list[str] = []
    if tool_name == "Write":
        parts.append(str(tool_input.get("content") or ""))
    elif tool_name == "Edit":
        parts.append(str(tool_input.get("new_string") or ""))
    elif tool_name == "MultiEdit":
        for e in tool_input.get("edits") or []:
            if isinstance(e, dict):
                parts.append(str(e.get("new_string") or ""))
    elif tool_name == "NotebookEdit":
        parts.append(str(tool_input.get("new_source") or ""))
    return "\n".join(parts)


def target_path(tool_input: dict) -> str | None:
    return tool_input.get("file_path") or tool_input.get("notebook_path")


def block(message: str) -> None:
    print(f"BLOCKED by agent-ops protection hook: {message}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    payload = json.load(sys.stdin)
    tool_name = payload.get("tool_name") or ""
    if tool_name not in EDIT_TOOLS:
        sys.exit(0)
    tool_input = payload.get("tool_input") or {}
    raw_path = target_path(tool_input)
    if not raw_path:
        sys.exit(0)
    # Project config comes from CLAUDE_PROJECT_DIR (the loader's default), NOT from the
    # tool call's cwd: an edit under a subdirectory must not change which config governs.
    cfg = hook_config.load_hooks_config()
    prot = cfg.get("protection") or {}
    tags = prot.get("tags") or {}

    path = pathlib.Path(str(raw_path)).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path

    for root in prot.get("readonly_roots") or []:
        try:
            root_p = pathlib.Path(str(root)).expanduser().resolve()
        except OSError:
            continue
        if resolved == root_p or root_p in resolved.parents:
            block(f"{resolved} is under the read-only root {root_p}. "
                  f"This path is configured as untouchable in hooks.toml.")

    protecting = {k: v for k, v in tags.items() if k in ("readonly", "ignore")}
    if path.is_file() and protecting:
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        hit = live_tags(existing, protecting)
        if hit:
            names = ", ".join(f"'{protecting[h]}'" for h in sorted(hit))
            block(f"{resolved} carries the {names} tag — it is owned by a human and must "
                  f"not be edited by an agent. Ask the user to remove the tag first.")

    added = new_content_of(tool_name, tool_input)
    if added:
        introduced = live_tags(added, {k: v for k, v in tags.items() if v})
        if introduced:
            names = ", ".join(f"'{tags[t]}'" for t in sorted(introduced))
            block(f"this edit introduces the bare protection tag(s) {names}. Agents must "
                  f"not grant or alter protection status; if you are only documenting the "
                  f"tag, put it in a code fence or inline code span.")

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:                                          # noqa: BLE001 — fail open
        sys.exit(0)
