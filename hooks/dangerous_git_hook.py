#!/usr/bin/env python3
"""PreToolUse hook: refuse destructive git commands, independent of permission mode.

Adapted from mattpocock/skills, skills/misc/git-guardrails-claude-code/scripts/
block-dangerous-git.sh @ 5b15a47 — MIT License, Copyright (c) 2026 Matt Pocock.
Upstream licence text: licenses/mattpocock-skills-MIT.txt. Ported bash → Python and
generalized: the pattern list is configurable, matching is token-wise rather than a
substring grep (so a commit MESSAGE that mentions `git push --force` no longer trips it),
heredoc bodies are stripped before matching, and `git -C <path>` plus per-worktree extra
blocks were added.

Config (hooks.toml, see hook_config.DEFAULTS):
  [dangerous_git]
  always_block     = ["reset --hard", "push --force", ...]   # matched on every git call
  shared_worktrees = ["~/somewhere"]  # under these roots, ALSO block: add -A,
                                      # commit --amend, rebase, reset

FAILS OPEN: any internal error exits 0. Guard rail, not security boundary — the block list
protects against a careless agent, not a malicious shell artist.

Exit 2 blocks the call; the stderr message is shown to the model.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_config  # noqa: E402

# Extra patterns inside a shared worktree: operations that are fine on a private clone but
# rewrite or bulk-stage state that other sessions may be relying on.
SHARED_WORKTREE_BLOCK = ["add -A", "commit --amend", "rebase", "reset"]

HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def strip_heredocs(command: str) -> str:
    """Remove heredoc BODIES so their content cannot trip (or hide behind) a pattern.

    The command line itself, including the `<<EOF` marker, is kept — only the quoted body
    between the marker line and the terminator goes.
    """
    lines = command.splitlines()
    out: list[str] = []
    terminator: str | None = None
    for line in lines:
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        out.append(line)
        m = HEREDOC_RE.search(line)
        if m:
            terminator = m.group(2)
    return "\n".join(out)


def split_segments(command: str) -> list[str]:
    """Break a compound command at shell separators so each simple command is judged on
    its own tokens. Parens and backticks are deliberately NOT separators: a regex split
    cannot see quoting, so splitting on them would cut a dangerous command in two
    whenever a quoted argument contains one (measured on a real audit of this file).
    Substitutions and subshells are handled token-wise in git_invocation() instead —
    shlex keeps quoted text as single tokens, which is what makes that safe."""
    return [s for s in re.split(r"(?:\|\||&&|;|\||\n)", command) if s.strip()]


def _is_git_token(tok: str) -> bool:
    """True for a token that puts git in command position, including inside a command
    substitution or subshell where the shell punctuation stays glued to the word:
    `git`, `$(git`, `` `git ``, `(git`, `VAR=$(git`, `/usr/bin/git`."""
    # An env assignment's value can itself open a substitution (`OUT=$(git ...`).
    tail = tok.split("=")[-1] if "=" in tok else tok
    tail = tail.lstrip("$(`")
    return tail == "git" or tail.endswith("/git")


def git_invocation(tokens: list[str]) -> tuple[list[str], str | None] | None:
    """If this token list invokes git, return (args-after-global-opts, -C path or None).

    Scans ALL tokens rather than only the command head: a `git` in the middle of a token
    list is a substitution/subshell invocation (`echo $(git ...)`). A git command merely
    MENTIONED inside a quoted string cannot false-positive here, because shlex collapses
    the quoted text into a single token that never equals bare `git`."""
    idx = None
    for i, tok in enumerate(tokens):
        if _is_git_token(tok):
            idx = i
            break
    if idx is None:
        return None
    args = tokens[idx + 1:]
    c_path: str | None = None
    while args:
        a = args[0]
        if a == "-C" and len(args) >= 2:
            c_path = args[1]
            args = args[2:]
        elif a == "-c" and len(args) >= 2:
            args = args[2:]
        elif a.startswith("--git-dir") or a.startswith("--work-tree"):
            args = args[2:] if "=" not in a and len(args) >= 2 else args[1:]
        elif a.startswith("-"):
            args = args[1:]
        else:
            break
    return args, c_path


def _token_matches(pattern_tok: str, arg_tok: str) -> bool:
    if pattern_tok == arg_tok:
        return True
    # A single-letter short flag also matches inside a cluster: `-f` must catch
    # `clean -fd` and `clean -fdx`, or the most common spellings sail through.
    if (re.fullmatch(r"-[A-Za-z]", pattern_tok)
            and re.fullmatch(r"-[A-Za-z]+", arg_tok)
            and pattern_tok[1] in arg_tok[1:]):
        return True
    return False


def matches(pattern: str, args: list[str]) -> bool:
    """Pattern tokens must appear IN ORDER among the git args (gaps allowed, so
    `push --force` catches `push origin --force`). Token-wise, so quoted strings that
    merely mention a command cannot trip it."""
    ptoks = pattern.split()
    if not ptoks:
        return False
    i = 0
    for arg in args:
        # A segment cut at `$(`/`(`/` ` `` keeps the substitution's closing delimiter
        # glued to its last token (`--hard)`); strip it so the token still matches.
        arg = arg.rstrip(")`")
        if _token_matches(ptoks[i], arg):
            i += 1
            if i == len(ptoks):
                return True
    return False


def in_shared_worktree(cwd: str | None, c_path: str | None, roots: list[str]) -> bool:
    if not roots:
        return False
    base = pathlib.Path(cwd or os.getcwd())
    target = base / c_path if c_path else base
    try:
        target = target.expanduser().resolve()
    except OSError:
        return False
    for root in roots:
        try:
            root_p = pathlib.Path(str(root)).expanduser().resolve()
        except OSError:
            continue
        if target == root_p or root_p in target.parents:
            return True
    return False


def check(command: str, cwd: str | None, cfg: dict) -> str | None:
    """Return a block message, or None to allow."""
    dg = cfg.get("dangerous_git") or {}
    always = [str(p) for p in (dg.get("always_block") or [])]
    roots = [str(r) for r in (dg.get("shared_worktrees") or [])]

    for segment in split_segments(strip_heredocs(command)):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue                       # unparseable segment: fail open on this piece
        inv = git_invocation(tokens)
        if inv is None:
            continue
        args, c_path = inv
        for pattern in always:
            if matches(pattern, args):
                return (f"'git {' '.join(args)}' matches the blocked pattern "
                        f"'{pattern}'. This operation destroys or rewrites work and is "
                        f"refused by the agent-ops dangerous-git hook. If it is genuinely "
                        f"needed, the user must run it themselves.")
        if in_shared_worktree(cwd, c_path, roots):
            for pattern in SHARED_WORKTREE_BLOCK:
                if matches(pattern, args):
                    return (f"'git {' '.join(args)}' matches '{pattern}', which is blocked "
                            f"inside a shared worktree: other sessions may depend on its "
                            f"state. Use a targeted alternative (explicit paths, a new "
                            f"commit, a branch).")
    return None


def main() -> None:
    payload = json.load(sys.stdin)
    if (payload.get("tool_name") or "") != "Bash":
        sys.exit(0)
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not command:
        sys.exit(0)
    # Project config comes from CLAUDE_PROJECT_DIR (the loader's default), NOT from the
    # tool call's cwd: a `cd` into a subdirectory must not change which config governs.
    cfg = hook_config.load_hooks_config()
    message = check(command, payload.get("cwd"), cfg)
    if message:
        print(f"BLOCKED by agent-ops dangerous-git hook: {message}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:                                          # noqa: BLE001 — fail open
        sys.exit(0)
