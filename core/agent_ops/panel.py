"""Seat selection: family identity, coder-family exclusion, roster-aware loading.

Never let a family review its own family's work. The exclusion is mechanical, not
advisory — `--coder` names the model that wrote the code and pick_panel drops that whole
family. This is what makes family diversity across seats load-bearing rather than
cosmetic: with one vendor, exclusion would leave nothing.
"""
from __future__ import annotations

import json
import pathlib
import re
import time

from .config import Config, Seat

ROSTER_MAX_AGE_DAYS = 14        # two full probe cycles: one missed probe is not staleness


def family_of(model: str) -> str:
    """Family identity of a model id, ignoring routing decoration.

    Handles provider-prefixed ids ('deepseek/deepseek-chat' -> 'deepseek') and plain names
    ('claude-sonnet-5' -> 'claude'). Used for the --coder side of exclusion; a SEAT's
    family is whatever the config declares, which always wins over inference.
    """
    m = (model or "").lower().strip()
    if "/" in m:                          # provider-qualified id: the vendor is the family
        return m.split("/", 1)[0]
    return re.split(r"[-.:]", m)[0]


def pick_panel(coder: str | None, n: int, override: list[str] | None,
               seats: list[Seat]) -> list[Seat]:
    """One seat per family, coder's family excluded, first-come ranked order.

    `override` names seats explicitly (by seat name or model id) and bypasses both the
    rotation and the exclusion — it is the escape hatch, and prints as such.
    """
    if override:
        wanted = [w.strip() for w in override if w.strip()]
        by_key = {}
        for s in seats:
            by_key.setdefault(s.name, s)
            by_key.setdefault(s.model, s)
        return [by_key[w] for w in wanted if w in by_key]
    banned = {family_of(coder)} if coder else set()
    out: list[Seat] = []
    seen: set[str] = set()
    for seat in seats:
        if seat.family in banned or seat.family in seen:
            continue
        out.append(seat)
        seen.add(seat.family)
        if len(out) == n:
            break
    return out


def load_probe_seconds(config: Config) -> dict[str, float]:
    """Seat name -> measured probe latency, from a FRESH roster only. Never raises.

    Probe latency is what lets the runner cap a dead-slow seat in minutes instead of
    letting it burn the whole budget (a probed-at-31s local seat once sat on the full
    900s before classifying dead). Freshness matters the same way it does for seat
    order: a measurement from a decommissioned endpoint is not a measurement.
    """
    try:
        raw = json.loads(pathlib.Path(config.roster_path).read_text(encoding="utf-8"))
        if (time.time() - float(raw["generated"])) / 86400 > ROSTER_MAX_AGE_DAYS:
            return {}
        return {str(r["seat"]): float(r["seconds"]) for r in raw.get("all", [])
                if r.get("seconds") and r.get("verdict") != "fail"}
    except Exception:                                          # noqa: BLE001
        return {}


def load_seats(config: Config) -> tuple[list[Seat], str]:
    """Return (seats, provenance). Never raises: a bad roster must not block a review.

    Order: fresh roster.json (written by `probe`, ranked by measurement) -> the config's
    own seat order. Every degraded case says so in the provenance string, because a silent
    fallback is exactly how a stale roster stays invisible.
    """
    by_name = {s.name: s for s in config.seats}
    roster_path = config.roster_path
    try:
        raw = json.loads(pathlib.Path(roster_path).read_text(encoding="utf-8"))
        age_days = (time.time() - float(raw["generated"])) / 86400
        if age_days > ROSTER_MAX_AGE_DAYS:
            return list(config.seats), (
                f"config seats (roster is {age_days:.0f}d old, limit {ROSTER_MAX_AGE_DAYS}d "
                f"— re-run `agent_ops probe`)")
        seats = [by_name[str(name)] for name, _fam in raw["seats"] if str(name) in by_name]
        dropped = [str(name) for name, _fam in raw["seats"] if str(name) not in by_name]
        # Fewer than two families cannot fill the default panel, and --coder exclusion
        # would empty it. A short roster is a probe failure, not a reason to run one seat.
        if len({s.family for s in seats}) < 2:
            return list(config.seats), (
                f"config seats (roster had only {len(seats)} usable seat(s))")
        prov = f"roster.json ({age_days:.1f}d old)"
        if dropped:
            # A roster naming seats that have since left the config must say so, or the
            # shrinkage is invisible until someone counts the panel by hand.
            prov += f" — {len(dropped)} roster seat(s) no longer in config: {', '.join(dropped)}"
        return seats, prov
    except FileNotFoundError:
        return list(config.seats), "config seats (no roster yet — `agent_ops probe` ranks them)"
    except Exception as e:                                    # noqa: BLE001
        return list(config.seats), f"config seats (roster unreadable: {type(e).__name__})"
