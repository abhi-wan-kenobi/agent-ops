"""Report writing: the prompt head, per-seat report files, PAYLOAD.txt, stats.jsonl.

Output shapes are part of the product: a `<family>.md` per seat with a loud banner when
the seat did not actually review anything, the exact outbound payload preserved beside
the reports, and one JSON line per run for long-term quality tracking.
"""
from __future__ import annotations

import json
import pathlib
import time

PROMPT_HEAD = """You are performing an adversarial code audit. Be skeptical and concrete.
You are NOT here to praise the code or summarise what it does.

Everything you need is inline below: the diff, and the full current text of every file it
touches. You have no tools and do not need any — do not ask to read anything.

Report ONLY defects that are real and that you can justify. For each finding give:

  SEVERITY: critical | high | medium | low
  FILE:LINE
  WHAT: one sentence stating the defect
  WHY:  the concrete failure — specific inputs or state that produce a wrong result, a
        crash, a leak, or data loss. If you cannot describe a concrete failure, the finding
        is speculation: drop it.
  FIX:  the minimal change that resolves it

Prioritise, in this order:
  1. Correctness — wrong results, unhandled cases, races, silent data loss. Silent wrongness
     is worse than a crash.
  2. Security — credential leakage, injection, unsafe permissions, over-broad access.
  3. Robustness — unhandled errors, missing validation, brittle parsing.
  4. Resource problems that would actually bite in production.

Explicitly ignore: formatting, naming, subjective style, missing docstrings, and "consider
adding a test" unless a specific untested path is genuinely dangerous.

End with a line exactly: AUDIT COMPLETE - <n> findings
A clean audit is a valid result; do not invent findings to appear thorough.
"""

# Which banner a status earns. A seat that did not review anything must SAY so at the top
# of its own report file, or a dead seat reads as a clean pass to whoever opens it.
def banner_for(status: str, reason: str) -> str:
    return {
        "truncated": ("# ⚠️ TRUNCATED — no AUDIT COMPLETE marker; findings count is inferred\n"
                      "#    from SEVERITY headers and may be short. Re-run narrower.\n"),
        "error": (f"# ⛔ SEAT DID NOT RUN — {reason}\n"
                  "#    This is NOT a clean review and NOT a finding of zero. Nothing was\n"
                  "#    reviewed by this seat; re-run it when the provider recovers.\n"),
        "timeout": (f"# ⛔ SEAT DID NOT RUN — {reason}\n"
                    "#    This is NOT a clean review and NOT a finding of zero.\n"),
        "empty": (f"# ⛔ SEAT REPORTED NOTHING — {reason}\n"
                  "#    An empty body is a dead seat, never agreement. NOT a clean review\n"
                  "#    and NOT a finding of zero.\n"),
    }.get(status, "")


def write_seat_report(outdir: pathlib.Path, family: str, model: str,
                      status: str, reason: str, out: str) -> pathlib.Path:
    path = outdir / f"{family}.md"
    path.write_text(f"# review seat: {model}\n" + banner_for(status, reason) + f"\n{out}\n",
                    encoding="utf-8")
    return path


def write_payload(outdir: pathlib.Path, payload: str) -> None:
    (outdir / "PAYLOAD.txt").write_text(payload, encoding="utf-8")


def append_stats(stats_path: pathlib.Path, *, run_id: str, repo_name: str, scope: str,
                 files: int, payload_chars: int, coder: str | None,
                 seats: list[dict], parent: str | None = None) -> None:
    """One line per run. `findings: null` for a seat that never ran — the difference
    between "looked and found nothing" and "never looked" must survive into the stats.

    A --split-by-file run writes one line per FILE (run id `<parent>/<subdir>`, `parent`
    set): each file convenes its own panel, and verdicts must land on the per-file
    reports the human actually read, not on an aggregate that has no report files."""
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        # `kind` discriminates run lines from verdict lines (stats.py) in the shared
        # file; v0.1 lines lack it, and readers must treat absent as "run".
        "ts": int(time.time()), "kind": "run", "run": run_id, "repo": repo_name,
        "scope": scope, "files": files, "payload_chars": payload_chars,
        "coder": coder, "seats": seats,
    }
    if parent:
        line["parent"] = parent
    with stats_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
