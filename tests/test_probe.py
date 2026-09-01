"""Probe: score configured seats on a known-defect diff, rank, write roster.json."""
from __future__ import annotations

import json

import pytest

from agent_ops import probe as probe_mod
from agent_ops.config import Config, ConfigError, ProviderConfig, Seat
from agent_ops.panel import load_seats
from agent_ops.probe import choose_panel, probe_seat, rank, run_probe
from agent_ops.providers import SeatOutput

GOOD_REPORT = ("SEVERITY: critical\nFILE: payments.py:5\nWHAT: division by zero\n\n"
               "SEVERITY: high\nFILE: payments.py:1\nWHAT: validation dropped\n\n"
               "AUDIT COMPLETE - 2 findings\n")
THIN_REPORT = "SEVERITY: high\nFILE: payments.py:5\nWHAT: one only\n\nAUDIT COMPLETE - 1 findings\n"


class StubProvider:
    """Scripted per-model outputs; records small-probe and stress calls separately
    (told apart by prompt size — the small probe prompt is ~1k chars, the stress prompt
    ≥ STRESS_TARGET_CHARS). Stress default is a GOOD report, so tests written before the
    stress stage keep their meaning: their seats simply survive it."""

    def __init__(self, outputs: dict[str, list[SeatOutput]],
                 stress_outputs: dict[str, list[SeatOutput]] | None = None):
        self.outputs = {m: list(outs) for m, outs in outputs.items()}
        self.stress_outputs = {m: list(o) for m, o in (stress_outputs or {}).items()}
        self.calls: list[str] = []
        self.stress_calls: list[str] = []
        self.ctx: dict[str, int] = {}

    def call(self, model, messages, *, max_tokens, temperature=None, timeout=0):
        if len(messages[0]["content"]) > 5000:
            self.stress_calls.append(model)
            outs = self.stress_outputs.get(model, [])
            if not outs:
                return SeatOutput(content=GOOD_REPORT)
            return outs.pop(0) if len(outs) > 1 else outs[0]
        self.calls.append(model)
        outs = self.outputs.get(model, [])
        return outs.pop(0) if len(outs) > 1 else (outs[0] if outs else SeatOutput(content=""))

    def context_lengths(self, timeout=15.0):
        return dict(self.ctx)


def _seat(name, family, model):
    return Seat(name=name, family=family, provider="stub", model=model)


def _config(tmp_path, seats) -> Config:
    return Config(outroot=tmp_path / "audits", state_dir=tmp_path / "state",
                  max_payload=400_000, max_tokens=8000, lease_slots=1, seats=seats,
                  providers={"stub": ProviderConfig(name="stub", type="openai-compatible",
                                                    base_url="http://unused/v1")})


@pytest.fixture()
def stub(monkeypatch):
    holder = {}

    def fake_make_provider(cfg):
        return holder["provider"]
    monkeypatch.setattr(probe_mod, "make_provider", fake_make_provider)
    return holder


# --- probe_seat verdicts -------------------------------------------------------------------

def test_good_verdict_requires_findings_and_terminator():
    p = StubProvider({"m": [SeatOutput(content=GOOD_REPORT, finish_reason="stop")]})
    row = probe_seat(p, _seat("s", "f", "m"), 8000)
    assert row["verdict"] == "good"
    assert row["findings"] == 2 and row["complete"] is True


def test_thin_verdict_when_fewer_defects_found():
    p = StubProvider({"m": [SeatOutput(content=THIN_REPORT)]})
    assert probe_seat(p, _seat("s", "f", "m"), 8000)["verdict"] == "thin"


def test_truncated_reply_is_thin_not_good():
    """Findings without the terminator means the reply was cut off — a partial review,
    never a clean one."""
    p = StubProvider({"m": [SeatOutput(content="SEVERITY: high\nSEVERITY: low\ncut off",
                                       finish_reason="length")]})
    row = probe_seat(p, _seat("s", "f", "m"), 8000)
    assert row["verdict"] == "thin"
    assert "truncated" in row["why"]


def test_empty_content_is_fail_with_reasoning_burn_named():
    p = StubProvider({"m": [SeatOutput(content="", reasoning="x" * 8000,
                                       finish_reason="length")]})
    row = probe_seat(p, _seat("s", "f", "m"), 8000)
    assert row["verdict"] == "fail"
    assert "8000 chars of reasoning" in row["why"]


def test_transport_error_is_fail_with_the_reason():
    p = StubProvider({"m": [SeatOutput(error="HTTP 429: slow down")]})
    row = probe_seat(p, _seat("s", "f", "m"), 8000)
    assert row["verdict"] == "fail"
    assert "429" in row["why"]


# --- ranking -------------------------------------------------------------------------------

def test_unknown_ctx_is_not_demoted_like_a_narrow_one():
    """ctx is None when the provider's listing doesn't advertise it — a property of the
    endpoint, not the model. Treating None as 0 would let a silent listing demote a
    working family below a measured-narrow model: the wrong direction to be wrong in."""
    rows = [
        {"model": "narrow-a", "seat": "a", "family": "fa", "verdict": "good",
         "findings": 3, "reasoning_chars": 10, "ctx_tokens": 32_000},
        {"model": "unknown-b", "seat": "b", "family": "fb", "verdict": "good",
         "findings": 3, "reasoning_chars": 10, "ctx_tokens": None},
        {"model": "roomy-c", "seat": "c", "family": "fc", "verdict": "good",
         "findings": 3, "reasoning_chars": 10, "ctx_tokens": 1_000_000},
    ]
    order = [r["model"] for r in rank(rows)]
    assert order.index("unknown-b") < order.index("narrow-a"), (
        f"unknown ctx ranked below a measured-narrow seat: {order}")
    assert order[-1] == "narrow-a", order
    assert order[0] == "roomy-c", order


def test_rank_prefers_findings_then_least_reasoning():
    rows = [
        {"model": "wasteful", "seat": "a", "family": "fa", "verdict": "good",
         "findings": 2, "reasoning_chars": 30_000, "ctx_tokens": None},
        {"model": "crisp", "seat": "b", "family": "fb", "verdict": "good",
         "findings": 2, "reasoning_chars": 0, "ctx_tokens": None},
        {"model": "sharp-eye", "seat": "c", "family": "fc", "verdict": "good",
         "findings": 4, "reasoning_chars": 20_000, "ctx_tokens": None},
        {"model": "failed", "seat": "d", "family": "fd", "verdict": "fail",
         "findings": 0, "reasoning_chars": 0, "ctx_tokens": None},
    ]
    order = [r["model"] for r in rank(rows)]
    assert order == ["sharp-eye", "crisp", "wasteful"], order


def test_choose_panel_dedupes_by_family_best_first():
    ranked = [
        {"seat": "a", "family": "deepseek"},
        {"seat": "a2", "family": "deepseek"},
        {"seat": "b", "family": "qwen"},
    ]
    assert [r["seat"] for r in choose_panel(ranked)] == ["a", "b"]


# --- run_probe end to end (stubbed provider) ------------------------------------------------

def test_run_probe_writes_a_ranked_deduped_roster(tmp_path, stub):
    seats = [_seat("s1", "fam-a", "model-a"), _seat("s2", "fam-b", "model-b"),
             _seat("s3", "fam-a", "model-a2")]
    stub["provider"] = StubProvider({
        "model-a": [SeatOutput(content=GOOD_REPORT)],
        "model-b": [SeatOutput(content=GOOD_REPORT)],
        "model-a2": [SeatOutput(content=GOOD_REPORT)],
    })
    cfg = _config(tmp_path, seats)
    rc = run_probe(cfg)
    assert rc == 0
    roster = json.loads(cfg.roster_path.read_text())
    fams = [f for _, f in roster["seats"]]
    assert sorted(fams) == ["fam-a", "fam-b"], "roster panel must be one seat per family"
    assert len(roster["all"]) == 3, "every probed seat appears in the roster, panel or not"
    # And the panel actually consumes it.
    loaded, prov = load_seats(cfg)
    assert {s.family for s in loaded} == {"fam-a", "fam-b"}
    assert "roster.json" in prov


def test_degraded_roster_exits_non_zero_but_still_writes(tmp_path, stub):
    """This may run unattended; exit 0 for a roster that cannot fill a panel would record
    success for a failure. The roster is still written — the data is informative, and
    load_seats already refuses a one-family roster."""
    seats = [_seat("s1", "fam-a", "model-a"), _seat("s2", "fam-a", "model-a2")]
    stub["provider"] = StubProvider({
        "model-a": [SeatOutput(content=GOOD_REPORT)],
        "model-a2": [SeatOutput(content=GOOD_REPORT)],
    })
    cfg = _config(tmp_path, seats)
    assert run_probe(cfg) == 1
    roster = json.loads(cfg.roster_path.read_text())
    assert len(roster["seats"]) == 1
    loaded, prov = load_seats(cfg)
    assert loaded == seats, "the panel must fall back to config seats on a short roster"


def test_no_seats_configured_is_exit_2(tmp_path, capsys):
    cfg = Config(outroot=tmp_path / "a", state_dir=tmp_path / "s", max_payload=1,
                 max_tokens=1, lease_slots=1, seats=[], providers={})
    assert run_probe(cfg) == 2


def test_non_good_seats_are_reprobed_once_and_the_better_result_kept(tmp_path, stub):
    """A false thin/fail silently removes a working family from every later panel; a false
    good merely promotes a model that mostly works. Retry the failures, keep the better."""
    seats = [_seat("flaky", "fam-a", "model-flaky"), _seat("solid", "fam-b", "model-solid")]
    stub["provider"] = StubProvider({
        "model-flaky": [SeatOutput(content=THIN_REPORT), SeatOutput(content=GOOD_REPORT)],
        "model-solid": [SeatOutput(content=GOOD_REPORT)],
    })
    cfg = _config(tmp_path, seats)
    assert run_probe(cfg) == 0
    assert stub["provider"].calls.count("model-flaky") == 2, "non-good must be retried once"
    assert stub["provider"].calls.count("model-solid") == 1, "good must not be re-spent"
    roster = json.loads(cfg.roster_path.read_text())
    flaky = next(r for r in roster["all"] if r["seat"] == "flaky")
    assert flaky["verdict"] == "good"
    assert "re-probe" in flaky["why"]


def test_a_worse_retry_does_not_overwrite_a_better_first_attempt(tmp_path, stub):
    seats = [_seat("s1", "fam-a", "model-a"), _seat("s2", "fam-b", "model-b")]
    stub["provider"] = StubProvider({
        "model-a": [SeatOutput(content=THIN_REPORT), SeatOutput(error="HTTP 500")],
        "model-b": [SeatOutput(content=GOOD_REPORT)],
    })
    cfg = _config(tmp_path, seats)
    run_probe(cfg)
    roster = json.loads(cfg.roster_path.read_text())
    row = next(r for r in roster["all"] if r["seat"] == "s1")
    assert row["verdict"] == "thin", "a fail on retry must not demote a thin first attempt"
    assert row.get("attempts") == 2


def test_missing_provider_key_fails_those_seats_loudly_not_silently(tmp_path, monkeypatch, capsys):
    def raising_make_provider(cfg):
        raise ConfigError(f"provider {cfg.name!r} needs THE_KEY and it is not set")
    monkeypatch.setattr(probe_mod, "make_provider", raising_make_provider)
    seats = [_seat("s1", "fam-a", "model-a")]
    cfg = _config(tmp_path, seats)
    rc = run_probe(cfg)
    assert rc == 1, "roster written, degraded"
    roster = json.loads(cfg.roster_path.read_text())
    assert roster["all"][0]["verdict"] == "fail"
    assert "THE_KEY" in roster["all"][0]["why"]
    assert "THE_KEY" in capsys.readouterr().err


# --- stress stage (v0.2.1, dogfood finding B) ------------------------------------------------

BURNED = SeatOutput(content="", reasoning="r" * 9000, finish_reason="length")


def test_build_stress_prompt_is_big_realistic_and_deterministic():
    from agent_ops.probe import STRESS_TARGET_CHARS, build_stress_prompt
    p = build_stress_prompt()
    assert len(p) >= STRESS_TARGET_CHARS
    assert "===== FULL FILE: payments.py =====" in p
    assert "load_refunds()" in p, "the two known defects must still be present"
    assert p == build_stress_prompt(), "must be comparable across runs"


def test_stress_silent_seat_is_demoted_out_of_the_panel(tmp_path, stub):
    """The finding: a seat probes good on ~1k chars, then burns its whole budget in
    reasoning on a real payload. Two-for-two measured; the stress stage must catch it."""
    seats = [_seat("burner", "fam-a", "model-burn"), _seat("solid", "fam-b", "model-b")]
    stub["provider"] = StubProvider(
        {"model-burn": [SeatOutput(content=GOOD_REPORT)],
         "model-b": [SeatOutput(content=GOOD_REPORT)]},
        stress_outputs={"model-burn": [BURNED]})
    cfg = _config(tmp_path, seats)
    rc = run_probe(cfg)
    assert rc == 1, "one usable family left — degraded, and the exit code must say so"
    roster = json.loads(cfg.roster_path.read_text())
    assert [f for _, f in roster["seats"]] == ["fam-b"], "the burner must leave the panel"
    row = next(r for r in roster["all"] if r["seat"] == "burner")
    assert row["verdict"] == "thin" and "DEMOTED by stress probe" in row["why"]
    assert row["stress"]["verdict"] == "fail"
    assert row["stress"]["reasoning_chars"] == 9000


def test_stress_fail_is_retried_once_and_recovery_keeps_the_seat(tmp_path, stub):
    seats = [_seat("flappy", "fam-a", "model-a"), _seat("solid", "fam-b", "model-b")]
    stub["provider"] = StubProvider(
        {"model-a": [SeatOutput(content=GOOD_REPORT)],
         "model-b": [SeatOutput(content=GOOD_REPORT)]},
        stress_outputs={"model-a": [BURNED, SeatOutput(content=GOOD_REPORT)]})
    cfg = _config(tmp_path, seats)
    assert run_probe(cfg) == 0
    assert stub["provider"].stress_calls.count("model-a") == 2
    roster = json.loads(cfg.roster_path.read_text())
    row = next(r for r in roster["all"] if r["seat"] == "flappy")
    assert row["verdict"] == "good" and row["stress"]["verdict"] == "good"


def test_stress_thin_is_recorded_but_does_not_demote(tmp_path, stub):
    """Answering-but-weak at stress size is degraded, not dead — only silence demotes,
    because silence is what reads as a clean review on real work."""
    seats = [_seat("s1", "fam-a", "model-a"), _seat("s2", "fam-b", "model-b")]
    stub["provider"] = StubProvider(
        {"model-a": [SeatOutput(content=GOOD_REPORT)],
         "model-b": [SeatOutput(content=GOOD_REPORT)]},
        stress_outputs={"model-a": [SeatOutput(content=THIN_REPORT)]})
    cfg = _config(tmp_path, seats)
    assert run_probe(cfg) == 0
    roster = json.loads(cfg.roster_path.read_text())
    row = next(r for r in roster["all"] if r["seat"] == "s1")
    assert row["verdict"] == "good"
    assert row["stress"]["verdict"] == "thin"


def test_only_good_seats_are_stress_probed(tmp_path, stub):
    seats = [_seat("weak", "fam-a", "model-a"), _seat("solid", "fam-b", "model-b")]
    stub["provider"] = StubProvider({
        "model-a": [SeatOutput(content=THIN_REPORT), SeatOutput(content=THIN_REPORT)],
        "model-b": [SeatOutput(content=GOOD_REPORT)],
    })
    cfg = _config(tmp_path, seats)
    run_probe(cfg)
    assert "model-a" not in stub["provider"].stress_calls, (
        "a seat that never passed the small probe has nothing to stress")
    assert stub["provider"].stress_calls == ["model-b"]
