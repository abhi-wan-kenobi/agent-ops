"""Close the loop as data: record verdicts on findings, report per-seat/per-coder rates.

The playbook's step 4 — "report what was confirmed and what was a false positive" — was
prose until v0.2. This makes it a record: `agent_ops verdict` appends one line per human
judgement to stats.jsonl, and `agent_ops stats` turns the accumulated log into the number
that trains you to read the panel correctly: each seat's measured false-positive rate.
A panel that reports its own FP rate is the discipline, not a dashboard.

Verdicts validate against and live in stats.jsonl rather than the run records, because
run records are GC'd after 14 days and quality tracking is exactly the thing that must
outlive them. The log is append-only; a re-judged finding is a new line, and the LAST
verdict for a (run, seat, finding) wins at read time — history preserved, mind allowed
to change.

Line shapes sharing one file, discriminated by `kind`:
  run lines     — written by report.append_stats; `kind` "run" (absent in v0.1 lines,
                  which must keep parsing: absent means run)
  verdict lines — {"kind": "verdict", ts, run, seat, family, model, coder, finding,
                  verdict: "confirmed"|"fp", note}
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

VERDICTS = ("confirmed", "fp")


def read_lines(stats_path: pathlib.Path) -> list[dict]:
    """Every parseable line, in file order. A corrupt line is skipped, not fatal: one bad
    write must not take the whole quality history with it."""
    try:
        raw = stats_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _is_run(rec: dict) -> bool:
    return rec.get("kind", "run") == "run" and "seats" in rec


def find_run(lines: list[dict], run_id: str) -> dict | None:
    for rec in lines:
        if _is_run(rec) and rec.get("run") == run_id:
            return rec
    return None


def resolve_seat(run: dict, key: str) -> dict | None:
    """Match a seat row by seat name, family, or model id — the human read the report as
    `<family>.md`, so the family is the name they most likely have in hand."""
    for field in ("seat", "family", "model"):
        for row in run.get("seats", []):
            if row.get(field) == key:
                return row
    return None


def append_verdict(stats_path: pathlib.Path, run_id: str, seat_key: str, finding: int,
                   verdict: str, note: str | None) -> tuple[int, str]:
    """Validate against the log, append one verdict line. Returns (exit_code, message).

    Validation is strict on purpose: a verdict that names no real run or seat would be
    unmatchable forever, silently poisoning every rate `stats` reports afterwards.
    """
    if verdict not in VERDICTS:
        return 2, f"verdict must be one of: {', '.join(VERDICTS)} (got {verdict!r})"
    if finding < 1:
        return 2, f"finding number must be >= 1 (got {finding}) — findings are counted " \
                  f"in report order, top to bottom"
    lines = read_lines(stats_path)
    run = find_run(lines, run_id)
    if run is None:
        known = [r.get("run") for r in lines if _is_run(r)]
        hint = f" — recent runs: {', '.join(known[-5:])}" if known else \
               " — no runs recorded yet"
        return 2, f"run {run_id!r} not found in {stats_path}{hint}"
    row = resolve_seat(run, seat_key)
    if row is None:
        names = ", ".join(sorted({r.get("family", "?") for r in run.get("seats", [])}))
        return 2, f"seat {seat_key!r} not in run {run_id} — its seats (by family): {names}"
    # A verdict must name a finding the seat actually emitted, or the rates aggregate
    # fabrications (panel finding, 2026-09-01). Two carve-outs: a dead seat has NOTHING to
    # judge (findings None ≠ zero findings), and a truncated report's count is inferred
    # and may be short, so an over-count verdict there is trusted rather than refused.
    emitted = row.get("findings")
    if emitted is None:
        return 2, (f"seat {row.get('family')} never produced a report in run {run_id} — "
                   f"there is no finding to judge")
    if finding > emitted and not row.get("truncated"):
        return 2, (f"seat {row.get('family')} emitted {emitted} finding(s) in run "
                   f"{run_id} — there is no finding {finding}")

    prior = None
    for rec in lines:
        if (rec.get("kind") == "verdict" and rec.get("run") == run_id
                and rec.get("seat") == row.get("seat") and rec.get("finding") == finding):
            prior = rec
    entry = {
        "ts": int(time.time()), "kind": "verdict", "run": run_id,
        # Denormalised on purpose: rates must be computable from this file alone,
        # long after the run record is GC'd.
        "seat": row.get("seat"), "family": row.get("family"), "model": row.get("model"),
        "coder": run.get("coder"), "finding": finding, "verdict": verdict, "note": note,
    }
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    with stats_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    msg = (f"recorded: {run_id} {row.get('family')} finding {finding} → {verdict}")
    if prior and prior.get("verdict") != verdict:
        msg += f" (supersedes earlier {prior.get('verdict')} — last verdict wins)"
    return 0, msg


def _effective_verdicts(lines: list[dict]) -> dict[tuple, dict]:
    """Last verdict per (run, seat, finding) wins."""
    out: dict[tuple, dict] = {}
    for rec in lines:
        if rec.get("kind") == "verdict":
            out[(rec.get("run"), rec.get("seat"), rec.get("finding"))] = rec
    return out


def summarize(lines: list[dict]) -> dict:
    """Aggregate the log into per-seat and per-coder tables.

    fp_rate divides by JUDGED findings only (confirmed + fp), never by findings emitted:
    an unjudged finding is not evidence in either direction, and counting it would reward
    seats whose findings nobody bothered to verify.
    """
    seats: dict[str, dict] = {}
    coders: dict[str, dict] = {}
    for rec in lines:
        if not _is_run(rec):
            continue
        coder = rec.get("coder") or "(unspecified)"
        c = coders.setdefault(coder, {"runs": 0, "findings": 0,
                                      "confirmed": 0, "fp": 0})
        c["runs"] += 1
        for row in rec.get("seats", []):
            s = seats.setdefault(row.get("seat", "?"),
                                 {"family": row.get("family"), "runs": 0, "dead": 0,
                                  "findings": 0, "confirmed": 0, "fp": 0})
            s["runs"] += 1
            if row.get("findings") is None:
                s["dead"] += 1          # never ran ≠ found nothing; count separately
            else:
                s["findings"] += row["findings"]
                c["findings"] += row["findings"]
    for v in _effective_verdicts(lines).values():
        # A hand-edited or half-written verdict line must not take `stats` down with a
        # KeyError — same tolerance read_lines gives unparseable lines. Panel finding,
        # 2026-09-01.
        if v.get("verdict") not in VERDICTS:
            continue
        s = seats.get(v.get("seat"))
        if s is not None:
            s[v["verdict"]] += 1
        coder = v.get("coder") or "(unspecified)"
        if coder in coders:
            coders[coder][v["verdict"]] += 1

    def rate(d: dict) -> float | None:
        judged = d["confirmed"] + d["fp"]
        return round(d["fp"] / judged, 3) if judged else None

    for d in list(seats.values()) + list(coders.values()):
        d["judged"] = d["confirmed"] + d["fp"]
        d["fp_rate"] = rate(d)
    return {"seats": seats, "coders": coders}


def _fmt_rate(d: dict) -> str:
    if d["fp_rate"] is None:
        return "—    "
    return f"{d['fp_rate'] * 100:3.0f}% "


def run_verdict(stats_path: pathlib.Path, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="agent_ops verdict",
        description="Record whether a finding held up: close the loop the playbook demands")
    ap.add_argument("run_id")
    ap.add_argument("seat", help="seat name, family (the report filename), or model id")
    ap.add_argument("finding", type=int, help="finding number, counted top-to-bottom "
                                              "in that seat's report")
    ap.add_argument("verdict", choices=list(VERDICTS),
                    help="confirmed = held up against the real code; fp = did not")
    ap.add_argument("--note", help="one line of why — future-you reading `stats` will ask")
    a = ap.parse_args(argv)
    code, msg = append_verdict(stats_path, a.run_id, a.seat, a.finding, a.verdict, a.note)
    print(msg, file=sys.stderr if code else sys.stdout)
    return code


def run_stats(stats_path: pathlib.Path, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="agent_ops stats",
        description="Per-seat and per-coder finding / false-positive rates")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    lines = read_lines(stats_path)
    summary = summarize(lines)
    if a.json:
        print(json.dumps(summary, indent=2))
        return 0
    if not summary["seats"]:
        print(f"no runs recorded yet in {stats_path}", file=sys.stderr)
        return 1
    print(f"{'seat':<14}{'family':<12}{'runs':>5}{'dead':>6}{'findings':>10}"
          f"{'judged':>8}{'conf':>6}{'fp':>4}  fp-rate")
    for name, d in sorted(summary["seats"].items()):
        print(f"{name:<14}{d['family'] or '?':<12}{d['runs']:>5}{d['dead']:>6}"
              f"{d['findings']:>10}{d['judged']:>8}{d['confirmed']:>6}{d['fp']:>4}  "
              f"{_fmt_rate(d)}")
    print()
    print(f"{'coder':<26}{'runs':>5}{'findings':>10}{'judged':>8}{'conf':>6}{'fp':>4}"
          f"  fp-rate")
    for name, d in sorted(summary["coders"].items()):
        print(f"{name:<26}{d['runs']:>5}{d['findings']:>10}{d['judged']:>8}"
              f"{d['confirmed']:>6}{d['fp']:>4}  {_fmt_rate(d)}")
    judged_total = sum(d["judged"] for d in summary["seats"].values())
    emitted_total = sum(d["findings"] for d in summary["seats"].values())
    if emitted_total and not judged_total:
        print("\nno verdicts recorded yet — after verifying a finding, close the loop:\n"
              "  python3 -m agent_ops verdict <run-id> <family> <n> confirmed|fp "
              "[--note why]", file=sys.stderr)
    return 0
