"""Verdict recording and rate reporting — the playbook's step 4 as data.

The properties that matter: a verdict must name a real run and seat (an unmatchable
verdict silently poisons every later rate), the last verdict for a finding wins (humans
change their minds), fp-rate divides by judged findings only, and v0.1 stats lines —
which predate the `kind` discriminator — must keep counting as runs.
"""
from __future__ import annotations

import json

import pytest

from agent_ops.report import append_stats
from agent_ops.stats import (append_verdict, read_lines, run_stats, run_verdict,
                             summarize)

SEATS = [
    {"model": "model-a", "seat": "seat-a", "family": "fam-a", "findings": 3,
     "status": "ok", "reason": "", "truncated": False, "seconds": 5.0, "chars": 100},
    {"model": "model-b", "seat": "seat-b", "family": "fam-b", "findings": None,
     "status": "timeout", "reason": "timeout", "truncated": False, "seconds": 900.0,
     "chars": 0},
]


@pytest.fixture()
def stats_path(tmp_path):
    p = tmp_path / "stats.jsonl"
    append_stats(p, run_id="run-1", repo_name="r", scope="uncommitted", files=1,
                 payload_chars=100, coder="opus", seats=SEATS)
    return p


def test_verdict_resolves_seat_by_family_and_denormalises(stats_path):
    # The human read the report as fam-a.md, so the family is the handle they have.
    code, msg = append_verdict(stats_path, "run-1", "fam-a", 2, "confirmed", "held up")
    assert code == 0 and "confirmed" in msg
    rec = read_lines(stats_path)[-1]
    assert rec["kind"] == "verdict"
    assert (rec["seat"], rec["family"], rec["model"]) == ("seat-a", "fam-a", "model-a")
    assert rec["coder"] == "opus"           # survives the run record's 14-day GC
    assert rec["finding"] == 2 and rec["note"] == "held up"


def test_verdict_on_unknown_run_is_refused_with_recent_runs_named(stats_path):
    code, msg = append_verdict(stats_path, "nope", "fam-a", 1, "fp", None)
    assert code == 2 and "run-1" in msg
    assert all(r.get("kind") != "verdict" for r in read_lines(stats_path))


def test_verdict_on_unknown_seat_names_the_real_ones(stats_path):
    code, msg = append_verdict(stats_path, "run-1", "fam-z", 1, "fp", None)
    assert code == 2 and "fam-a" in msg and "fam-b" in msg


def test_finding_numbers_start_at_one(stats_path):
    code, _ = append_verdict(stats_path, "run-1", "fam-a", 0, "fp", None)
    assert code == 2


def test_last_verdict_wins(stats_path):
    append_verdict(stats_path, "run-1", "fam-a", 1, "fp", "looked fake")
    code, msg = append_verdict(stats_path, "run-1", "fam-a", 1, "confirmed", "was real")
    assert code == 0 and "supersedes" in msg
    s = summarize(read_lines(stats_path))["seats"]["seat-a"]
    assert (s["confirmed"], s["fp"]) == (1, 0)


def test_fp_rate_divides_by_judged_not_emitted(stats_path):
    # 3 findings emitted, only 2 judged: 1 confirmed + 1 fp → 50%, not 1/3.
    append_verdict(stats_path, "run-1", "fam-a", 1, "confirmed", None)
    append_verdict(stats_path, "run-1", "fam-a", 2, "fp", None)
    s = summarize(read_lines(stats_path))["seats"]["seat-a"]
    assert s["findings"] == 3 and s["judged"] == 2 and s["fp_rate"] == 0.5


def test_dead_seat_counts_as_dead_never_as_zero_findings(stats_path):
    s = summarize(read_lines(stats_path))["seats"]["seat-b"]
    assert s["dead"] == 1 and s["findings"] == 0 and s["fp_rate"] is None


def test_v01_lines_without_kind_still_count_as_runs(tmp_path):
    p = tmp_path / "stats.jsonl"
    p.write_text(json.dumps({"ts": 1, "run": "old-run", "repo": "r", "scope": "s",
                             "files": 1, "payload_chars": 9, "coder": "opus",
                             "seats": SEATS}) + "\n", encoding="utf-8")
    code, _ = append_verdict(p, "old-run", "fam-a", 1, "confirmed", None)
    assert code == 0
    assert summarize(read_lines(p))["coders"]["opus"]["runs"] == 1


def test_corrupt_lines_are_skipped_not_fatal(stats_path):
    with stats_path.open("a", encoding="utf-8") as fh:
        fh.write("{half a line\n")
    append_stats(stats_path, run_id="run-2", repo_name="r", scope="s", files=1,
                 payload_chars=5, coder="opus", seats=SEATS)
    assert summarize(read_lines(stats_path))["coders"]["opus"]["runs"] == 2


def test_per_coder_rates_aggregate_across_runs(stats_path):
    append_stats(stats_path, run_id="run-2", repo_name="r", scope="s", files=1,
                 payload_chars=5, coder="gpt", seats=SEATS)
    append_verdict(stats_path, "run-1", "fam-a", 1, "fp", None)
    append_verdict(stats_path, "run-2", "fam-a", 1, "confirmed", None)
    coders = summarize(read_lines(stats_path))["coders"]
    assert coders["opus"]["fp"] == 1 and coders["opus"]["confirmed"] == 0
    assert coders["gpt"]["confirmed"] == 1 and coders["gpt"]["fp"] == 0


def test_stats_cli_renders_tables_and_json(stats_path, capsys):
    append_verdict(stats_path, "run-1", "fam-a", 1, "confirmed", None)
    assert run_stats(stats_path, []) == 0
    out = capsys.readouterr().out
    assert "seat-a" in out and "fp-rate" in out and "coder" in out
    assert run_stats(stats_path, ["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["seats"]["seat-a"]["confirmed"] == 1


def test_stats_cli_with_no_history_says_so(tmp_path, capsys):
    assert run_stats(tmp_path / "stats.jsonl", []) == 1
    assert "no runs recorded" in capsys.readouterr().err


def test_verdict_cli_round_trip(stats_path, capsys):
    rc = run_verdict(stats_path, ["run-1", "fam-a", "1", "confirmed", "--note", "why"])
    assert rc == 0
    assert "recorded" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run_verdict(stats_path, ["run-1", "fam-a", "1", "maybe"])  # not a valid verdict
