"""Family identity, coder exclusion, and roster-aware seat loading."""
from __future__ import annotations

import json
import time

from agent_ops.config import Config, Seat
from agent_ops.panel import (ROSTER_MAX_AGE_DAYS, family_of, load_probe_seconds,
                             load_seats, pick_panel)


def _seat(name, family, model=None):
    return Seat(name=name, family=family, provider="p", model=model or f"{family}/{name}")


SEATS = [
    _seat("seat-a", "deepseek", "deepseek/deepseek-chat"),
    _seat("seat-b", "qwen", "qwen/qwen3-coder"),
    _seat("seat-c", "mistral", "mistralai/mistral-small"),
    _seat("local", "llama", "llama3.1"),
]


def _config(tmp_path, seats=None) -> Config:
    return Config(outroot=tmp_path / "audits", state_dir=tmp_path / "state",
                  max_payload=400_000, max_tokens=8000, lease_slots=1,
                  seats=list(SEATS if seats is None else seats), providers={})


# --- family_of ---------------------------------------------------------------------------

def test_family_of_provider_qualified_ids():
    assert family_of("deepseek/deepseek-chat") == "deepseek"
    assert family_of("mistralai/mistral-small-3.2-24b-instruct") == "mistralai"
    assert family_of("qwen/qwen3-coder") == "qwen"


def test_family_of_plain_names():
    assert family_of("claude-sonnet-5") == "claude"
    assert family_of("llama3.1") == "llama3"
    assert family_of("gpt-oss:120b") == "gpt"
    assert family_of("opus") == "opus"


# --- pick_panel --------------------------------------------------------------------------

def test_coder_family_is_mechanically_excluded():
    """Never let a family review its own family's work — by machinery, not convention."""
    panel = pick_panel("deepseek/deepseek-chat", 2, None, SEATS)
    assert [s.name for s in panel] == ["seat-b", "seat-c"]
    assert all(s.family != "deepseek" for s in panel)


def test_one_seat_per_family():
    seats = SEATS + [_seat("seat-a2", "deepseek"), _seat("seat-b2", "qwen")]
    panel = pick_panel(None, 4, None, seats)
    fams = [s.family for s in panel]
    assert len(fams) == len(set(fams)), f"families collapsed: {fams}"


def test_panel_size_is_honoured():
    assert len(pick_panel(None, 2, None, SEATS)) == 2
    assert len(pick_panel(None, 10, None, SEATS)) == len(SEATS), "asks for more than exist"


def test_override_bypasses_rotation_and_matches_name_or_model():
    panel = pick_panel("deepseek", 2, ["seat-a", "qwen/qwen3-coder"], SEATS)
    assert [s.name for s in panel] == ["seat-a", "seat-b"], (
        "override must match by seat name AND by model id, and ignore coder exclusion")


def test_coder_none_excludes_nothing():
    assert len(pick_panel(None, 4, None, SEATS)) == 4


# --- load_seats --------------------------------------------------------------------------

def _write_roster(cfg: Config, names_fams, generated=None):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.roster_path.write_text(json.dumps({
        "generated": generated if generated is not None else time.time(),
        "seats": names_fams,
    }), encoding="utf-8")


def test_fresh_roster_orders_the_config_seats(tmp_path):
    cfg = _config(tmp_path)
    _write_roster(cfg, [["local", "llama"], ["seat-c", "mistral"]])
    seats, prov = load_seats(cfg)
    assert [s.name for s in seats] == ["local", "seat-c"]
    assert "roster.json" in prov


def test_stale_roster_falls_back_to_config_and_says_so(tmp_path):
    cfg = _config(tmp_path)
    old = time.time() - (ROSTER_MAX_AGE_DAYS + 1) * 86400
    _write_roster(cfg, [["local", "llama"], ["seat-c", "mistral"]], generated=old)
    seats, prov = load_seats(cfg)
    assert seats == SEATS
    assert "old" in prov and "probe" in prov, prov


def test_missing_roster_falls_back_to_config(tmp_path):
    cfg = _config(tmp_path)
    seats, prov = load_seats(cfg)
    assert seats == SEATS
    assert "no roster yet" in prov


def test_corrupt_roster_falls_back_without_raising(tmp_path):
    cfg = _config(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.roster_path.write_text("{ not json", encoding="utf-8")
    seats, prov = load_seats(cfg)
    assert seats == SEATS
    assert "unreadable" in prov


def test_roster_naming_unknown_seats_is_a_degraded_roster(tmp_path):
    """A roster written before seats were renamed must not shrink the panel silently."""
    cfg = _config(tmp_path)
    _write_roster(cfg, [["deleted-seat", "kimi"], ["seat-a", "deepseek"]])
    seats, prov = load_seats(cfg)
    assert seats == SEATS, "one usable family cannot fill a two-seat panel"
    assert "1 usable" in prov, prov


def test_single_family_roster_is_refused(tmp_path):
    """Fewer than two families cannot fill the default panel, and --coder exclusion would
    empty it. A short roster is a probe failure, not a reason to review with one seat."""
    cfg = _config(tmp_path)
    _write_roster(cfg, [["seat-a", "deepseek"]])
    seats, prov = load_seats(cfg)
    assert seats == SEATS
    assert "usable" in prov


def test_roster_seats_missing_from_config_are_named_in_the_provenance(tmp_path):
    """Audit finding: a roster naming since-removed seats shrank the panel with no clue in
    the provenance line."""
    cfg = _config(tmp_path)
    _write_roster(cfg, [["seat-a", "deepseek"], ["seat-b", "qwen"], ["renamed-away", "kimi"]])
    seats, prov = load_seats(cfg)
    assert [s.name for s in seats] == ["seat-a", "seat-b"]
    assert "renamed-away" in prov, prov


# --- load_probe_seconds ------------------------------------------------------------------

def _write_roster_with_detail(cfg: Config, all_rows, generated=None):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.roster_path.write_text(json.dumps({
        "generated": generated if generated is not None else time.time(),
        "seats": [[r["seat"], "fam"] for r in all_rows],
        "all": all_rows,
    }), encoding="utf-8")


def test_probe_seconds_come_from_a_fresh_roster(tmp_path):
    cfg = _config(tmp_path)
    _write_roster_with_detail(cfg, [
        {"seat": "seat-a", "seconds": 31.2, "verdict": "good"},
        {"seat": "seat-b", "seconds": 55.0, "verdict": "thin"},
    ])
    assert load_probe_seconds(cfg) == {"seat-a": 31.2, "seat-b": 55.0}


def test_probe_seconds_exclude_failed_seats(tmp_path):
    """A fail row's seconds measure the failure (a transport error can return in 0.1s, a
    timeout at the probe cap), not the seat's working latency — capping from it would be
    capping from noise."""
    cfg = _config(tmp_path)
    _write_roster_with_detail(cfg, [
        {"seat": "seat-a", "seconds": 31.2, "verdict": "good"},
        {"seat": "dead", "seconds": 0.1, "verdict": "fail"},
    ])
    assert load_probe_seconds(cfg) == {"seat-a": 31.2}


def test_probe_seconds_from_a_stale_roster_are_ignored(tmp_path):
    cfg = _config(tmp_path)
    old = time.time() - (ROSTER_MAX_AGE_DAYS + 1) * 86400
    _write_roster_with_detail(cfg, [{"seat": "seat-a", "seconds": 31.2,
                                     "verdict": "good"}], generated=old)
    assert load_probe_seconds(cfg) == {}


def test_probe_seconds_missing_or_corrupt_roster_is_empty_not_fatal(tmp_path):
    cfg = _config(tmp_path)
    assert load_probe_seconds(cfg) == {}
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.roster_path.write_text("{nope", encoding="utf-8")
    assert load_probe_seconds(cfg) == {}


def test_probe_seconds_zero_is_a_measurement_not_missing(tmp_path):
    """Panel finding, 2026-09-01: a falsy check dropped a 0-second measurement."""
    cfg = _config(tmp_path)
    _write_roster_with_detail(cfg, [{"seat": "seat-a", "seconds": 0,
                                     "verdict": "good"}])
    assert load_probe_seconds(cfg) == {"seat-a": 0.0}
