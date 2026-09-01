"""Full lifecycle wiring through main(): CLI, run records, reports, quorum warnings.

The provider layer is faked at the make_provider seam so no network call ever happens;
everything else — config, payload, lease, run_state, classification, reports — is real.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import textwrap
import time

import pytest

from agent_ops import main as main_mod
from agent_ops import run_state
from agent_ops.main import claim_run_dir, main
from agent_ops.providers import SeatOutput

GOOD_REPORT = ("SEVERITY: high\nFILE: a.py:2\nWHAT: broken\nWHY: because\nFIX: fix\n\n"
               "AUDIT COMPLETE - 1 findings\n")


class FakeProvider:
    """Scripted outputs per model; optionally slow; optionally lists models."""

    outputs: dict[str, SeatOutput] = {}
    delays: dict[str, float] = {}
    listed: set[str] | None = None      # None = listing endpoint unreachable
    on_call = None                      # hook: called with the model name

    def __init__(self, cfg):
        self.cfg = cfg

    def call(self, model, messages, *, max_tokens, temperature=None, timeout=0):
        if FakeProvider.on_call:
            FakeProvider.on_call(model)
        time.sleep(FakeProvider.delays.get(model, 0))
        return FakeProvider.outputs.get(model, SeatOutput(content=GOOD_REPORT))

    def model_ids(self, timeout=15.0):
        return FakeProvider.listed

    def context_lengths(self, timeout=15.0):
        return {}


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    FakeProvider.outputs = {}
    FakeProvider.delays = {}
    FakeProvider.listed = None
    FakeProvider.on_call = None
    monkeypatch.setattr(main_mod, "make_provider", lambda cfg: FakeProvider(cfg))
    yield FakeProvider


@pytest.fixture()
def env(tmp_path):
    """A git repo with an uncommitted change, and a panel.toml pointing state at tmp."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "base")
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    cfg = tmp_path / "panel.toml"
    cfg.write_text(textwrap.dedent(f"""
        [agent_ops]
        outroot = "{tmp_path / 'audits'}"
        state_dir = "{tmp_path / 'state'}"

        [[seats]]
        name = "seat-a"
        family = "fam-a"
        provider = "p"
        model = "model-a"

        [[seats]]
        name = "seat-b"
        family = "fam-b"
        provider = "p"
        model = "model-b"

        [providers.p]
        type = "openai-compatible"
        base_url = "http://unused.invalid/v1"
    """), encoding="utf-8")
    return repo, cfg, tmp_path


def _argv(repo, cfg, *extra):
    return [str(repo), "--config", str(cfg), "--coder", "some-other-family", *extra]


def test_clean_panel_end_to_end(env, capsys):
    repo, cfg, tmp = env
    rc = main(_argv(repo, cfg))
    assert rc == 0
    err = capsys.readouterr().err
    assert "seats from: config seats (no roster yet" in err
    assert "2+ seats agreeing" in err

    runs = run_state.list_runs()
    assert len(runs) == 1
    rec = runs[0]
    assert rec["state"] == "done" and rec["exit_code"] == 0
    assert rec["started_at"] is not None and rec["finished_at"] is not None
    assert rec["coder"] == "some-other-family"
    assert len(rec["panel"]) == 2
    assert all(s["status"] == "ok" and s["findings"] == 1 for s in rec["panel"])
    assert rec["lease"]["held"] is False, "a finished run must not still claim the lease"

    outdir = pathlib.Path(rec["outdir"])
    assert (outdir / "PAYLOAD.txt").is_file()
    assert (outdir / "fam-a.md").is_file() and (outdir / "fam-b.md").is_file()
    assert "review seat: model-a" in (outdir / "fam-a.md").read_text()

    stats = (tmp / "state" / "stats.jsonl").read_text().strip().splitlines()
    assert len(stats) == 1
    line = json.loads(stats[0])
    assert line["run"] == rec["run_id"]
    assert {s["model"] for s in line["seats"]} == {"model-a", "model-b"}


def test_coder_family_never_reviews_its_own_work(env, fake_provider):
    repo, cfg, _ = env
    called: list[str] = []
    fake_provider.on_call = called.append
    rc = main([str(repo), "--config", str(cfg), "--coder", "fam-a/some-model"])
    assert rc == 0
    assert called == ["model-b"], "the coder's family must be mechanically excluded"


def test_dead_seats_mean_failure_not_a_clean_pass(env, fake_provider, capsys):
    repo, cfg, _ = env
    fake_provider.outputs = {
        "model-a": SeatOutput(error="HTTP 429: capped"),
        "model-b": SeatOutput(error="timeout"),
    }
    rc = main(_argv(repo, cfg))
    assert rc == 1, "a dead panel must exit non-zero — a scripted caller must not record a pass"
    err = capsys.readouterr().err
    assert "NO SEAT PRODUCED A REPORT" in err
    rec = run_state.list_runs()[0]
    assert rec["state"] == "failed"
    statuses = {s["model"]: s["status"] for s in rec["panel"]}
    assert statuses == {"model-a": "error", "model-b": "timeout"}


def test_single_reporting_seat_prints_the_quorum_warning(env, fake_provider, capsys):
    repo, cfg, _ = env
    fake_provider.outputs = {"model-b": SeatOutput(error="HTTP 500: down")}
    rc = main(_argv(repo, cfg))
    assert rc == 0, "one live seat is still a (degraded) run"
    err = capsys.readouterr().err
    assert "ONLY 1 of 2 seats reported" in err
    assert "LEAD" in err


def test_empty_body_claim_is_a_dead_seat_in_the_record(env, fake_provider):
    repo, cfg, _ = env
    fake_provider.outputs = {"model-a": SeatOutput(content="AUDIT COMPLETE - 9 findings\n")}
    main(_argv(repo, cfg))
    rec = run_state.list_runs()[0]
    seat = next(s for s in rec["panel"] if s["model"] == "model-a")
    assert seat["status"] == "empty"
    assert seat["findings"] is None, "a fabricated count must not enter the record"
    banner = pathlib.Path(rec["outdir"], "fam-a.md").read_text()
    assert "SEAT REPORTED NOTHING" in banner


def test_no_diff_is_exit_1_with_a_note(env, capsys):
    repo, cfg, _ = env
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "all committed"], cwd=repo, check=True)
    rc = main(_argv(repo, cfg))
    assert rc == 1
    assert "nothing to review" in capsys.readouterr().err


def test_secret_in_payload_refuses_before_anything_leaves(env, fake_provider, capsys):
    repo, cfg, _ = env
    called: list[str] = []
    fake_provider.on_call = called.append
    (repo / "a.py").write_text("token = '" + "ghp_" + "a" * 24 + "'\n", encoding="utf-8")
    rc = main(_argv(repo, cfg))
    assert rc == 3
    assert "REFUSED" in capsys.readouterr().err
    assert called == [], "no seat may be called once the gate fires"


def test_unroutable_seat_is_dropped_with_a_loud_line(env, fake_provider, capsys):
    repo, cfg, _ = env
    fake_provider.listed = {"model-a"}          # provider lists only model-a
    rc = main(_argv(repo, cfg))
    assert rc == 0
    err = capsys.readouterr().err
    assert "NOT ROUTABLE" in err and "model-b" in err
    assert "ONLY 1 of 1 seats reported" in err, (
        "a panel shrunk by unroutability is degraded and must say so")


def test_all_seats_unroutable_is_a_refusal(env, fake_provider, capsys):
    repo, cfg, _ = env
    fake_provider.listed = set()
    rc = main(_argv(repo, cfg))
    assert rc == 2
    assert "NO ROUTABLE SEAT" in capsys.readouterr().err


def test_unreachable_listing_endpoint_does_not_block(env, fake_provider):
    repo, cfg, _ = env
    fake_provider.listed = None                 # could not ask
    assert main(_argv(repo, cfg)) == 0


def test_models_override_is_explicit_in_the_output(env, capsys):
    repo, cfg, _ = env
    rc = main([str(repo), "--config", str(cfg), "--models", "seat-b"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "seats from: --models (explicit)" in err


def test_missing_coder_warns_loudly(env, capsys):
    repo, cfg, _ = env
    main([str(repo), "--config", str(cfg)])
    assert "no --coder given" in capsys.readouterr().err


def test_cancel_mid_panel_abandons_the_slow_seat(env, fake_provider, capsys):
    repo, cfg, tmp = env
    fake_provider.delays = {"model-b": 5.0}

    def cancel_on_first_call(model):
        if model == "model-a":
            for p in (tmp / "state" / "runs").glob("*.json"):
                run_state.request_cancel(p.stem, note="human hit cancel")
    fake_provider.on_call = cancel_on_first_call

    t0 = time.monotonic()
    rc = main(_argv(repo, cfg))
    elapsed = time.monotonic() - t0
    assert rc == 8, "a cancelled run must exit with its own distinct code"
    assert elapsed < 3.0, f"cancellation must abandon the slow seat, not wait for it ({elapsed}s)"
    rec = run_state.list_runs()[0]
    assert rec["state"] == "cancelled"
    assert rec["note"] == "human hit cancel"
    slow = next(s for s in rec["panel"] if s["model"] == "model-b")
    assert slow["status"] == "abandoned"


def test_lease_denied_is_exit_7(env, monkeypatch, capsys):
    repo, cfg, _ = env
    monkeypatch.setattr(main_mod, "acquire_lease_cooperatively",
                        lambda *a, **k: (False, False))
    rc = main(_argv(repo, cfg))
    assert rc == 7
    assert "holds the lease" in capsys.readouterr().err
    rec = run_state.list_runs()[0]
    assert rec["state"] == "failed"


def test_no_lock_skips_the_lease(env):
    repo, cfg, _ = env
    rc = main(_argv(repo, cfg, "--no-lock"))
    assert rc == 0
    rec = run_state.list_runs()[0]
    assert rec["lease"]["held"] is False, "--no-lock must not claim to hold a lease"


def test_cancel_signal_handler_writes_a_terminal_record_and_raises(env):
    run_state.RUNS_DIR = env[2] / "state" / "runs"
    run_state.new_run("sig1", "/tmp/out", "/repo", "uncommitted", None)
    handler = main_mod._make_cancel_signal_handler("sig1")
    with pytest.raises(SystemExit) as exc:
        handler(15, None)                       # 15 == SIGTERM, simulated directly
    assert exc.value.code == 130
    rec = run_state.read_run("sig1")
    assert rec["state"] == "cancelled"
    assert "signal 15" in rec["error"]


def test_claim_run_dir_never_hands_out_the_same_id_twice(tmp_path):
    """Three runs in the same second used to collide on a bare timestamp id: one record
    kept, two runs' history overwritten, four report files destroyed."""
    ids = set()
    for _ in range(5):
        rid, d = claim_run_dir(tmp_path)
        assert d.is_dir()
        ids.add(rid)
    assert len(ids) == 5, f"colliding run ids: {ids}"


def test_missing_config_file_is_a_clean_config_error(env, capsys):
    repo, _, _ = env
    rc = main([str(repo), "--config", "/nonexistent/panel.toml"])
    assert rc == 2
    assert "CONFIG ERROR" in capsys.readouterr().err


def test_runs_subcommand_lists_via_the_configured_state_dir(env, capsys):
    repo, cfg, _ = env
    main(_argv(repo, cfg))
    capsys.readouterr()
    rc = main(["runs", "list", "--config", str(cfg)])
    assert rc == 0
    assert "done" in capsys.readouterr().out


def test_probe_subcommand_writes_the_roster(env, monkeypatch, capsys):
    repo, cfg, tmp = env
    from agent_ops import probe as probe_mod
    monkeypatch.setattr(probe_mod, "make_provider", lambda c: FakeProvider(c))
    FakeProvider.outputs = {
        "model-a": SeatOutput(content="SEVERITY: a\nSEVERITY: b\nAUDIT COMPLETE - 2 findings"),
        "model-b": SeatOutput(content="SEVERITY: a\nSEVERITY: b\nAUDIT COMPLETE - 2 findings"),
    }
    rc = main(["probe", "--config", str(cfg)])
    assert rc == 0
    roster = json.loads((tmp / "state" / "roster.json").read_text())
    assert len(roster["seats"]) == 2


# --- regressions from the 2026-09-01 adversarial audit of this port ------------------------

def test_a_crashing_seat_becomes_an_error_seat_not_a_dead_panel(env, fake_provider, capsys):
    """Audit finding (confirmed by two families): an exception escaping a seat runner
    killed the whole panel and left the run record permanently 'running'. A crashed seat
    must become an honest `error` seat while the other seat still reports."""
    repo, cfg, _ = env

    def boom(model):
        if model == "model-a":
            raise RuntimeError("disk full while writing the report")
    fake_provider.on_call = boom
    rc = main(_argv(repo, cfg))
    assert rc == 0, "the surviving seat still reported"
    rec = run_state.list_runs()[0]
    assert rec["state"] == "done", "the run must reach a terminal state"
    crashed = next(s for s in rec["panel"] if s["model"] == "model-a")
    assert crashed["status"] == "error"
    assert crashed["findings"] is None
    assert "ONLY 1 of 2 seats reported" in capsys.readouterr().err


def test_unknown_models_override_names_warn_instead_of_vanishing(env, capsys):
    """Audit finding: a typo in --models silently shrank the panel."""
    repo, cfg, _ = env
    rc = main([str(repo), "--config", str(cfg), "--models", "seat-a,seat-typo"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "'seat-typo'" in err and "matches no configured seat" in err


# --- probe-informed per-seat timeouts (v0.2) ----------------------------------------------

def _mkseat(name):
    from agent_ops.config import Seat
    return Seat(name=name, family=f"fam-{name}", provider="p", model=f"model-{name}")


def test_seat_timeouts_cap_at_multiplier_with_floor_and_ceiling():
    from agent_ops.main import (DEFAULT_TIMEOUT, TIMEOUT_FLOOR, TIMEOUT_MULTIPLIER,
                                seat_timeouts)
    panel = [_mkseat("fast"), _mkseat("mid"), _mkseat("slow"), _mkseat("unprobed")]
    probed = {"fast": 5.0, "mid": 60.0, "slow": 400.0}
    t = seat_timeouts(panel, probed, explicit=None)
    assert t["fast"] == TIMEOUT_FLOOR, "a fast probe must not strangle a real review"
    assert t["mid"] == 60 * TIMEOUT_MULTIPLIER
    assert t["slow"] == DEFAULT_TIMEOUT, "probe-informed is a cap, never an extension"
    assert t["unprobed"] == DEFAULT_TIMEOUT


def test_explicit_timeout_overrides_probe_data():
    from agent_ops.main import seat_timeouts
    t = seat_timeouts([_mkseat("fast")], {"fast": 5.0}, explicit=333)
    assert t["fast"] == 333


def test_probed_timeouts_are_named_in_the_run_output(env, capsys):
    """The v0.2 friction this fixes: a probed-at-31s seat burned the full 900s before
    classifying dead, with nothing in the output explaining what cap applied."""
    repo, cfg, tmp = env
    state = tmp / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "roster.json").write_text(json.dumps({
        "generated": time.time(),
        "seats": [["seat-a", "fam-a"], ["seat-b", "fam-b"]],
        "all": [{"seat": "seat-a", "seconds": 10.0, "verdict": "good"},
                {"seat": "seat-b", "seconds": 60.0, "verdict": "good"}],
    }), encoding="utf-8")
    rc = main(_argv(repo, cfg))
    assert rc == 0
    err = capsys.readouterr().err
    assert "seat-a 120s (probed)" in err
    assert "seat-b 360s (probed)" in err
