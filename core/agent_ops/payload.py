"""Build the exact text a seat reviews: git diff + full text of every touched file.

Scoped payload, not repo access. The seat gets NO tools; everything it may read is inlined
here, which bounds the review to one shot and makes the secret gate meaningful — the scan
in main() runs over precisely this string, because this string is what leaves the machine.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

DEFAULT_MAX_PAYLOAD = 400_000     # chars of diff+files; beyond this we truncate and SAY so

# Above this, a soft note suggests splitting with --only. Deliberately NOT a refusal: an
# earlier incarnation refused large payloads, blaming size for empty reports, and that
# diagnosis was wrong — heavy-reasoning seats return nothing at ANY size. The seat is the
# variable, not the byte count.
SEAT_NOTE_CHARS = 20_000


def run_git(args: list[str], cwd: pathlib.Path) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ""


def split_diff_blocks(diff: str) -> list[tuple[str | None, str]]:
    """Split a unified diff into (path, block) pairs, one per file, in diff order.

    Path comes from the '+++ b/<path>' header. For a deleted file that header is
    '+++ /dev/null', so fall back to the '--- a/<path>' line — otherwise deletions get a
    None path, never reach the files list, and a deletion-only scope reads as "no diff".
    Only the first '--- a/' per block is trusted (it precedes '+++' in the header; later
    matches could be removed content lines), and it is used only when '+++' is /dev/null.
    """
    blocks: list[tuple[str | None, str]] = []
    cur: list[str] = []
    path: str | None = None
    old_path: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if cur:
                blocks.append((path, "\n".join(cur)))
            cur, path, old_path = [line], None, None
        else:
            cur.append(line)
        if old_path is None and line.startswith("--- a/"):
            old_path = line[6:]
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line == "+++ /dev/null" and path is None:
            path = old_path
    if cur:
        blocks.append((path, "\n".join(cur)))
    return blocks


def build_payload(repo: pathlib.Path, scope: str, only: str | None = None,
                  max_payload: int = DEFAULT_MAX_PAYLOAD, *,
                  exact: bool = False) -> tuple[str, list[str], str]:
    """Return (payload, files, description). Payload = diff + full text of changed files.

    `only` narrows to files whose path contains that substring — essential, not optional,
    for multi-file changes: splitting is the difference between a real review and one that
    silently overruns a seat. `exact` makes it a whole-path match instead: --split-by-file
    feeds paths taken FROM the diff back in, and substring matching would silently drag a
    second file along whenever one path contains another (x.py vs x.py.orig).

    A brand-new file is the one case `only` cannot help, because a new file's diff IS its
    full text with every line prefixed '+', so inlining both doubles the payload for zero
    extra information. The hunks of pure additions are dropped and only the FULL FILE copy
    kept — restricted to files readable on disk, since for an unreadable new file the hunks
    are the change's only record and dropping both would hide it entirely.
    """
    if scope == "uncommitted":
        diff, desc = run_git(["diff", "HEAD"], repo), "uncommitted changes"
    elif scope == "last":
        diff, desc = run_git(["show", "--format=", "HEAD"], repo), "the last commit"
    elif scope.startswith("commit:"):
        ref = scope.split(":", 1)[1]
        diff, desc = run_git(["show", "--format=", ref], repo), f"commit {ref}"
    else:
        diff, desc = run_git(["diff", scope], repo), f"changes vs {scope}"

    blocks = split_diff_blocks(diff)
    files = sorted({p for p, _ in blocks if p})
    if only:
        files = [f for f in files if (f == only if exact else only in f)]
        desc += f" (only {only!r})" if exact else f" (only files matching {only!r})"
        # Keep just the hunks for the selected files, so the diff shrinks with the file
        # list rather than dragging the whole change along and defeating the point.
        blocks = [(p, b) for p, b in blocks if p in files]

    on_disk = {f for f in files if (repo / f).is_file()}
    added = {p for p, b in blocks
             if p in on_disk and re.search(r"^new file mode ", b, re.M)}
    blocks = [(p, b) for p, b in blocks if p not in added]
    diff = "\n".join(b for _, b in blocks)
    if added:
        desc += f" ({len(added)} new file(s) shown in full rather than as '+' lines)"

    if diff.strip():
        parts = [f"===== DIFF ({desc}) =====\n{diff}"]
    else:
        # Every file in scope is new. Say so, rather than presenting an empty DIFF section
        # that reads like the change was somehow lost.
        parts = [f"===== DIFF ({desc}) =====\n[No modification hunks: every file in this "
                 f"scope is newly added, so its complete text below IS the change.]"]
    for f in files:
        p = repo / f
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.append(f"\n===== FULL FILE: {f} =====\n{text}")
    payload = "\n".join(parts)
    if len(payload) > max_payload:
        payload = payload[:max_payload]
        payload += (f"\n\n[TRUNCATED at {max_payload} chars — this review did NOT see the "
                    f"whole change. Narrow the scope and re-run for full coverage.]")
    return payload, files, desc
