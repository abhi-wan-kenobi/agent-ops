"""The machine-readable summary, with the emphasis on the paths that produce no report.

A caller automating the panel needs to tell "reviewed everything, found nothing" from
"never reviewed anything" — playbook rule 1. The happy path is the easy half; these tests
exist mostly for the exits that have no run directory and no stats line.
"""
from __future__ import annotations

import json
import pathlib
import signal
import subprocess

import pytest

from agent_ops.main import main
from agent_ops.providers import SeatOutput
from agent_ops.summary import SCHEMA, Summary

from test_main import FakeProvider, _argv, env, fake_provider   # noqa: F401


def _read(p: pathlib.Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def test_summary_is_written_beside_the_reports_without_the_flag(env):
    """The run directory is where a human looks; the document belongs next to PAYLOAD.txt
    whether or not anyone asked for it by path."""
    repo, cfg, root = env
    assert main(_argv(repo, cfg)) == 0
    runs = list((root / "audits").iterdir())
    assert len(runs) == 1, runs
    doc = _read(runs[0] / "summary.json")
    assert doc["outcome"] == "done"
    assert doc["schema"] == SCHEMA
    assert doc["reported_seats"] == 2
    assert doc["panel_size"] == 2
    assert doc["files"] == ["a.py"]
    assert (runs[0] / "PAYLOAD.txt").is_file()


def test_summary_flag_is_written_when_there_is_no_run_directory(env, tmp_path):
    """The whole point of the flag. 'no diff' produces no run dir, no stats line and no
    reports, so without an explicit path a caller gets nothing to distinguish it from a
    clean review."""
    repo, cfg, _ = env
    out = tmp_path / "nested" / "s.json"
    rc = main(_argv(repo, cfg, "--only", "no-such-file-anywhere",
                    "--summary-json", str(out)))
    assert rc == 1
    doc = _read(out)
    assert doc["outcome"] == "no-diff"
    assert doc["exit_code"] == 1
    assert doc["reported_seats"] == 0
    assert doc["run_id"] is None, "nothing to review claims no run directory"


def test_summary_records_a_refused_secret(env, tmp_path):
    repo, cfg, _ = env
    (repo / "a.py").write_text(
        "TOKEN = 'ghp_" + "a" * 30 + "'\n", encoding="utf-8")
    out = tmp_path / "s.json"
    rc = main(_argv(repo, cfg, "--summary-json", str(out)))
    assert rc == 3
    doc = _read(out)
    assert doc["outcome"] == "refused-secret"
    assert doc["exit_code"] == 3
    assert doc["run_id"] is None, "a refusal happens before any run dir is claimed"


def test_summary_records_an_unresolvable_ref(env, tmp_path):
    repo, cfg, _ = env
    out = tmp_path / "s.json"
    rc = main(_argv(repo, cfg, "--scope", "origin/nope...HEAD", "--summary-json", str(out)))
    assert rc == 2
    doc = _read(out)
    assert doc["outcome"] == "git-error"
    assert doc["files"] == []


def test_summary_records_a_dead_panel_as_failed_not_clean(env, tmp_path):
    """The failure this whole tool exists to make visible: every seat died, so nothing was
    reviewed. `done` here would let a CI gate record a dead panel as a pass."""
    repo, cfg, _ = env
    FakeProvider.outputs = {"model-a": SeatOutput(error="boom"),
                            "model-b": SeatOutput(error="timeout")}
    out = tmp_path / "s.json"
    rc = main(_argv(repo, cfg, "--summary-json", str(out)))
    assert rc == 1
    doc = _read(out)
    assert doc["outcome"] == "failed"
    assert doc["reported_seats"] == 0
    assert doc["max_severity"] is None
    assert all(s["findings"] is None for s in doc["seats"])


def test_summary_carries_max_severity_for_the_run_and_each_seat(env, tmp_path):
    repo, cfg, _ = env
    FakeProvider.outputs = {
        "model-a": SeatOutput(content="SEVERITY: low\nFILE: a.py:1\nWHAT: x\n\n"
                                      "AUDIT COMPLETE - 1 findings\n"),
        "model-b": SeatOutput(content="SEVERITY: high\nFILE: a.py:2\nWHAT: y\n\n"
                                      "AUDIT COMPLETE - 1 findings\n")}
    out = tmp_path / "s.json"
    assert main(_argv(repo, cfg, "--summary-json", str(out))) == 0
    doc = _read(out)
    assert doc["max_severity"] == "high", "run severity must be the worst across seats"
    by_family = {s["family"]: s for s in doc["seats"]}
    assert by_family["fam-a"]["max_severity"] == "low"
    assert by_family["fam-b"]["max_severity"] == "high"
    assert by_family["fam-a"]["report"].endswith("fam-a.md")


def test_split_by_file_summary_lists_every_file_with_its_verdict_id(env, tmp_path):
    """Verdicts land per file, so the document has to name the per-file run id — a
    consumer rendering findings must tell the reader which id to record against."""
    repo, cfg, _ = env
    (repo / "b.py").write_text("z = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    out = tmp_path / "s.json"
    main(_argv(repo, cfg, "--split-by-file", "--summary-json", str(out)))
    doc = _read(out)
    assert doc["split"] is not None
    files = {e["file"]: e for e in doc["split"]}
    assert set(files) == {"a.py", "b.py"}, files
    for f, e in files.items():
        assert e["status"] == "reviewed", e
        assert e["run_id"] == f"{doc['run_id']}/{e['sub']}", e
        assert e["seats"], e


def test_summary_is_emitted_once_even_if_two_paths_try(tmp_path):
    """The signal handler and a normal return can both fire on the way out of a cancelled
    run. The first description of the run wins; a later one must not overwrite it."""
    out = tmp_path / "s.json"
    s = Summary(repo="/x", scope="uncommitted", path=out)
    assert s.emit("cancelled", 130) == 130
    assert s.emit("done", 0) == 0, "emit must still return the code it was given"
    assert _read(out)["outcome"] == "cancelled", "the second emit overwrote the first"


def test_a_summary_that_cannot_be_written_does_not_crash_the_run(tmp_path, capsys):
    """A completed review must not be turned into a crash by an unwritable path."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    s = Summary(repo="/x", scope="uncommitted", path=blocker / "sub" / "s.json")
    assert s.emit("done", 0) == 0
    assert "could not write summary" in capsys.readouterr().err


def test_signal_handler_emits_a_cancelled_summary(env, tmp_path, monkeypatch):
    """A CI job cancelled mid-review sends SIGTERM. No summary file at all would be
    indistinguishable from the tool never having started."""
    repo, cfg, root = env
    out = tmp_path / "s.json"
    fired = {"done": False}

    def kill_mid_flight(model):
        if not fired["done"]:
            fired["done"] = True
            os_signal = signal.getsignal(signal.SIGTERM)
            os_signal(signal.SIGTERM, None)

    FakeProvider.on_call = kill_mid_flight
    with pytest.raises(SystemExit) as e:
        main(_argv(repo, cfg, "--summary-json", str(out)))
    assert e.value.code == 130
    doc = _read(out)
    assert doc["outcome"] == "cancelled"
    assert doc["exit_code"] == 130
    assert doc["run_id"], "a cancelled run still knows its own id"
