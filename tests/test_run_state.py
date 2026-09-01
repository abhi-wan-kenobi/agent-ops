"""Durable run records: atomicity, concurrency, GC, cancellation.

Every test redirects run_state.RUNS_DIR to a tmp_path — the real state dir is shared,
host-wide, and must never be touched by a test.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import time

import pytest

from agent_ops import run_state

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="fcntl/proc semantics are POSIX")


@pytest.fixture(autouse=True)
def _isolated_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "RUNS_DIR", tmp_path / "runs")
    yield


def test_new_run_is_queued_and_carries_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-abc123")
    rec = run_state.new_run("r0", str(tmp_path / "out"), "/some/repo", "uncommitted", "sonnet")
    assert rec["state"] == "queued"
    assert rec["started_by"]["session_id"] == "sess-abc123"
    assert rec["started_by"]["pid"] == os.getpid()
    assert rec["started_by"]["repo"] == "/some/repo"
    assert rec["started_by"]["scope"] == "uncommitted"
    assert rec["started_at"] is None and rec["finished_at"] is None
    assert run_state.read_run("r0") == rec, "what's on disk must match what new_run returned"


def test_session_identity_is_honest_when_env_is_unset(monkeypatch, tmp_path):
    """No session id in the environment must record None, not a made-up id."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    rec = run_state.new_run("r1", "/tmp/out", "/repo", "last", None)
    assert rec["started_by"]["session_id"] is None


def test_update_run_returns_none_for_a_run_that_no_longer_exists():
    assert run_state.update_run("does-not-exist", state="running") is None


def test_finish_run_sets_terminal_state_and_timestamp():
    run_state.new_run("r1", "/tmp/out", "/repo", "uncommitted", None)
    rec = run_state.finish_run("r1", "done", exit_code=0)
    assert rec["state"] == "done"
    assert rec["exit_code"] == 0
    assert rec["finished_at"] is not None


def test_finish_run_rejects_a_non_terminal_state():
    run_state.new_run("r1", "/tmp/out", "/repo", "uncommitted", None)
    with pytest.raises(AssertionError):
        run_state.finish_run("r1", "queued")


def test_set_panel_and_update_seat_patches_only_the_matching_model():
    run_state.new_run("r1", "/tmp/out", "/repo", "uncommitted", None)
    run_state.set_panel("r1", [("deepseek/deepseek-chat", "deepseek"), ("qwen/qwen3", "qwen")])
    run_state.update_seat("r1", "deepseek/deepseek-chat", status="ok", findings=1, seconds=12.3)
    rec = run_state.read_run("r1")
    a = next(s for s in rec["panel"] if s["model"] == "deepseek/deepseek-chat")
    b = next(s for s in rec["panel"] if s["model"] == "qwen/qwen3")
    assert (a["status"], a["findings"], a["seconds"]) == ("ok", 1, 12.3)
    assert (b["status"], b["findings"]) == ("pending", None), (
        "updating one seat must not touch the other")


def test_cancel_requested_roundtrip():
    run_state.new_run("r1", "/tmp/out", "/repo", "uncommitted", None)
    assert run_state.cancel_requested("r1") is False
    run_state.request_cancel("r1", note="stuck, please stop")
    assert run_state.cancel_requested("r1") is True
    assert run_state.read_run("r1")["note"] == "stuck, please stop"


def test_cancel_requested_is_false_for_a_run_that_does_not_exist():
    assert run_state.cancel_requested("nope") is False


def test_atomic_write_leaves_no_temp_file_behind(tmp_path):
    run_state.new_run("r1", "/tmp/out", "/repo", "uncommitted", None)
    run_state.update_run("r1", state="running")
    leftovers = [p.name for p in (tmp_path / "runs").iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"atomic write left temp files behind: {leftovers}"


# --- staleness / GC: verify liveness, never trust a timestamp ----------------------------

def test_gc_reaps_a_running_record_whose_owning_pid_is_dead(monkeypatch):
    run_state.new_run("r1", "/tmp/out", "/repo", "uncommitted", None)
    run_state.update_run("r1", state="running", started_at=time.time())

    def _dead_kill(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr(run_state.os, "kill", _dead_kill)
    run_state.gc_runs()
    rec = run_state.read_run("r1")
    assert rec["state"] == "failed"
    assert "orphaned" in rec["error"]
    assert rec["finished_at"] is not None


def test_gc_leaves_a_running_record_alone_when_its_pid_is_alive():
    run_state.new_run("r1", "/tmp/out", "/repo", "uncommitted", None)
    run_state.update_run("r1", state="running", started_at=time.time())
    # started_by.pid is os.getpid() (this test process) — genuinely alive.
    run_state.gc_runs()
    assert run_state.read_run("r1")["state"] == "running", "a live owner must not be reaped"


def test_gc_prunes_terminal_records_past_the_retention_window(tmp_path):
    run_state.new_run("r1", "/tmp/out", "/repo", "uncommitted", None)
    run_state.finish_run("r1", "done", exit_code=0)
    # Backdate on disk: update_run() deliberately refuses to patch a terminal record, so
    # a test may not use it to age one. Writing the file is the honest way to fake time.
    old = time.time() - (run_state.TERMINAL_RETENTION_DAYS + 1) * 86400
    rec = run_state.read_run("r1"); rec["finished_at"] = old
    (tmp_path / "runs" / "r1.json").write_text(json.dumps(rec), encoding="utf-8")
    run_state.gc_runs()
    assert run_state.read_run("r1") is None, "a stale terminal record must be pruned"


def test_gc_keeps_recent_terminal_records():
    run_state.new_run("r1", "/tmp/out", "/repo", "uncommitted", None)
    run_state.finish_run("r1", "done", exit_code=0)
    run_state.gc_runs()
    assert run_state.read_run("r1") is not None, "a fresh terminal record must survive GC"


def test_list_runs_runs_gc_and_reports_age():
    run_state.new_run("r1", "/tmp/out", "/repo", "uncommitted", None)
    runs = run_state.list_runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "r1"
    assert runs[0]["age_seconds"] >= 0


def test_list_runs_on_an_empty_directory_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(run_state, "RUNS_DIR", tmp_path / "does-not-exist-yet")
    assert run_state.list_runs() == []


# --- concurrent seat updates --------------------------------------------------------------
# The panel runs seats in THREADS. A PID-suffixed temp name broke here: two threads derive
# the same temp path, the first os.replace() renames it away, the second raises
# FileNotFoundError mid-run. The read-modify-write was unlocked too, so two seats landing
# together dropped one update — a tracker that under-reports what ran.

def test_concurrent_seat_updates_neither_crash_nor_lose_an_update():
    seats = [(f"model-{i}", f"fam{i}") for i in range(12)]
    run_state.new_run("concurrency", "/tmp/out", "/repo", "uncommitted", "opus")
    run_state.set_panel("concurrency", seats)
    errors = []

    def land(seat):
        try:
            run_state.update_seat("concurrency", seat[0], status="ok", findings=1, seconds=0.1)
        except Exception as e:                                    # noqa: BLE001
            errors.append(e)

    with cf.ThreadPoolExecutor(max_workers=len(seats)) as ex:
        list(ex.map(land, seats))

    assert not errors, f"concurrent writes raised: {errors!r}"
    panel = run_state.read_run("concurrency")["panel"]
    assert len(panel) == len(seats)
    unfinished = [s["model"] for s in panel if s.get("status") != "ok"]
    assert not unfinished, f"updates were lost for {unfinished}"


def test_the_temp_file_name_is_unique_per_call(monkeypatch):
    """A PID-derived name is not unique across threads — that is what broke."""
    seen = set()
    real = run_state.tempfile.mkstemp

    def spy(*a, **k):
        fd, path = real(*a, **k)
        seen.add(path)
        return fd, path

    monkeypatch.setattr(run_state.tempfile, "mkstemp", spy)
    run_state.new_run("unique", "/tmp/out", "/repo", "s", "opus")
    for i in range(5):
        run_state.update_run("unique", note=f"n{i}")
    assert len(seen) >= 5, f"temp names reused: {seen}"


def test_no_temp_or_lock_files_are_reported_as_runs():
    run_state.new_run("tidy", "/tmp/out", "/repo", "s", "opus")
    run_state.update_run("tidy", note="x")
    ids = [r["run_id"] for r in run_state.list_runs()]
    assert ids == ["tidy"], ids


# --- lease bookkeeping in the record -------------------------------------------------------

def test_a_finished_run_does_not_still_claim_the_lease():
    """Found live: a completed run's record read `lease HELD` while the lease itself was
    free. The tracker misreporting the one thing it exists to show is the failure this
    whole store was written to avoid."""
    run_state.new_run("leasey", "/tmp/out", "/repo", "uncommitted", "opus")
    run_state.update_run("leasey", state="running",
                         lease={"held": True, "waiting_since": None, "held_since": 1.0})
    rec = run_state.finish_run("leasey", "done", exit_code=0)
    assert rec["lease"]["held"] is False
    assert rec["lease"]["released_at"] is not None
    assert rec["lease"]["held_since"] == 1.0, "history of when it held must survive"


def test_finishing_a_run_that_never_held_the_lease_is_harmless():
    run_state.new_run("waiter", "/tmp/out", "/repo", "uncommitted", "opus")
    run_state.update_run("waiter", lease={"held": False, "waiting_since": 5.0,
                                          "held_since": None})
    rec = run_state.finish_run("waiter", "cancelled", note="user cancelled while queued")
    assert rec["lease"]["held"] is False
    assert rec["lease"]["waiting_since"] is None, "a terminal run is not still waiting"
    assert "released_at" not in rec["lease"], "never held, so nothing was released"


def test_a_terminal_record_cannot_be_resurrected_by_a_late_seat():
    """Cancellation abandons seats but cannot stop threads already in flight. A seat
    landing afterwards must not patch — or with a `state` field, revive — a run the human
    was already told was cancelled."""
    run_state.new_run("late", "/tmp/out", "/repo", "uncommitted", "opus")
    run_state.set_panel("late", [("deepseek/deepseek-chat", "deepseek")])
    run_state.finish_run("late", "cancelled", note="human hit cancel")
    run_state.update_seat("late", "deepseek/deepseek-chat", status="ok", findings=3)
    run_state.update_run("late", state="running")
    rec = run_state.read_run("late")
    assert rec["state"] == "cancelled", "a late write revived a terminal run"
    assert rec["panel"][0]["status"] == "pending", "a late seat patched a terminal run"


def test_cancelling_a_finished_run_reports_too_late_rather_than_success():
    """Cancelling a finished run is not an error; claiming to have cancelled one is."""
    run_state.new_run("fin", "/tmp/out", "/repo", "uncommitted", "opus")
    assert run_state.request_cancel("fin") is not None, "a live run must be cancellable"
    run_state.finish_run("fin", "done", exit_code=0)
    assert run_state.request_cancel("fin") is None
    assert run_state.request_cancel("nonexistent") is None


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="pid start-time check reads /proc")
def test_pid_reuse_cannot_make_a_dead_run_look_alive(tmp_path):
    """A pid alone is not an identity; (pid, start_time) is."""
    run_state.new_run("reused", "/tmp/out", "/repo", "uncommitted", "opus")
    run_state.update_run("reused", state="running")
    rec = run_state.read_run("reused")
    # Same pid (this live process), but the start time of an earlier, now-dead occupant.
    rec["started_by"]["pid_start_time"] = 1
    (tmp_path / "runs" / "reused.json").write_text(json.dumps(rec), encoding="utf-8")
    run_state.gc_runs()
    assert run_state.read_run("reused")["state"] == "failed", "pid reuse read as alive"


def test_a_genuinely_live_run_survives_gc():
    run_state.new_run("live", "/tmp/out", "/repo", "uncommitted", "opus")
    run_state.update_run("live", state="running")
    run_state.gc_runs()
    assert run_state.read_run("live")["state"] == "running", "GC reaped a live run"


def test_orphaned_lock_sidecars_are_pruned(tmp_path):
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    stray = tmp_path / "runs" / "gone.json.lock"
    stray.write_text("", encoding="utf-8")
    run_state.gc_runs()
    assert not stray.exists(), "lock sidecars accumulate forever"


def test_runs_cli_list_show_cancel(capsys):
    run_state.new_run("clirun", "/tmp/out", "/repo", "uncommitted", None)
    assert run_state.cli(["list"]) == 0
    assert "clirun" in capsys.readouterr().out
    assert run_state.cli(["show", "clirun"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "clirun"
    assert run_state.cli(["cancel", "clirun", "--note", "why"]) == 0
    capsys.readouterr()
    assert run_state.cancel_requested("clirun") is True
    run_state.finish_run("clirun", "done")
    assert run_state.cli(["cancel", "clirun"]) == 1, "too late must not report success"
