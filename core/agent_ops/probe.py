"""Probe the configured seats and record which are actually usable, ranked.

WHY THIS EXISTS. A hand-maintained seat list goes stale silently, and a stale review
roster fails in the worst possible direction: a seat that returns nothing prints
"0 findings", which reads as a clean review. Availability is nowhere near sufficient
either — a reachable model may still be useless as a seat, because heavy-reasoning models
spend the whole output budget in the reasoning channel and emit an empty report. So each
configured seat gets a real review-shaped prompt containing TWO known defects, and is
scored on the findings it actually emits, not on whether it answered.

Three verdicts:
  good — emitted >= EXPECT_FINDINGS findings and the terminator line
  thin — answered, but found fewer defects than are known to be in the probe diff
  fail — transport error, or empty content (the reasoning-burn failure mode)

Two stages, because one is not enough. Reasoning burn scales with input, and the probe
diff is five lines — a seat can score `good` here and still return nothing on the first
real payload (measured twice on the same seat, 2026-09-01: clean probes, then empty
content with the whole budget in `reasoning` at 9k and 11k chars). So every seat that
passes the small probe is probed AGAIN at STRESS_TARGET_CHARS — the same two defects
inside a realistically sized payload. A seat that goes silent at stress size is demoted
out of the panel: it would die exactly when it matters, on real work, while printing
nothing anywhere a roster could see.

This is an explicit command (`agent_ops probe`), not a timer: scheduling belongs to the
user. It writes roster.json; the panel reads that when fresh and falls back to the
config's own seat order otherwise, so stale/missing/corrupt degrades to a known state
rather than to an empty panel.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import sys
import time

from .config import Config, ConfigError, Seat
from .providers import BaseProvider, make_provider

EXPECT_FINDINGS = 2      # the probe diff below contains exactly two defects
PROBE_TIMEOUT = 300

# ⚠️ CALIBRATION. Do not lower max_tokens for probing below the panel's own budget: a
# too-tight cap does not measure reasoning burn, it MANUFACTURES it — models get cut off
# mid-reasoning and score `fail` while being perfectly healthy.

# A diff with exactly two defects, both concrete:
#   1. ZeroDivisionError when load_refunds() is empty
#   2. the amt <= 0 validation was dropped, so negative refunds are accepted
PROBE_DIFF = """--- a/payments.py
+++ b/payments.py
@@ -1,6 +1,9 @@
-def refund(amt):
-    if amt <= 0:
-        raise ValueError("refund must be positive")
-    return process(amt)
+def refund(amt):
+    total = 0
+    for r in load_refunds():
+        total += r
+    return total / len(load_refunds())
"""

PROMPT = f"""You are performing an adversarial code audit. Be skeptical and concrete.
Report ONLY defects that are real. For each finding give exactly:

  SEVERITY: critical | high | medium | low
  FILE:LINE
  WHAT: one sentence stating the defect
  WHY: the concrete failure - specific inputs or state producing a wrong result or crash
  FIX: the minimal change that resolves it

Ignore formatting, naming and style. End with a line exactly:
AUDIT COMPLETE - <n> findings

===== DIFF =====
{PROBE_DIFF}
"""

# What a probe actually measured: latency on THIS many chars of prompt. A cap derived
# from it must scale before being applied to a payload orders of magnitude larger —
# dogfood finding 2026-09-01: a constant 6x of probe latency killed a healthy seat at
# 120s that finishes the same real payload in 113s.
PROBE_PROMPT_CHARS = len(PROMPT)

# Stress-stage size. Chosen from measurement, not taste: the seat that motivated the
# stage probed `good` and then burned its whole budget at 9,116 and 11,080 chars, so the
# stress payload sits just past where the cliff was actually observed.
STRESS_TARGET_CHARS = 12_000

# Deliberately mundane code, repeated with varied numbers until the target size: the
# stress payload must look like a real review payload (diff + FULL FILE sections), and
# padding with lorem-ipsum would let a model skip it as obviously irrelevant.
_FILLER_BLOCK = '''
def load_batch_%(n)d(rows, limit=%(limit)d):
    """Filter and normalise batch %(n)d before posting to the ledger."""
    out = []
    for r in rows:
        if r.get("state") != "ready" or r.get("amount") is None:
            continue
        amount = round(float(r["amount"]), 2)
        out.append({"id": r["id"], "amount": amount, "bucket": r["id"] %% limit})
    if len(out) > limit:
        out = out[:limit]
    return out
'''


def build_stress_prompt(target_chars: int = STRESS_TARGET_CHARS) -> str:
    """The small probe's two known defects, embedded in a payload of realistic size and
    shape. Deterministic: same output every call, so results are comparable across runs."""
    filler: list[str] = []
    n = 0
    while sum(len(b) for b in filler) < target_chars:
        n += 1
        filler.append(_FILLER_BLOCK % {"n": n, "limit": 10 + n})
    # PROMPT already ends with the ===== DIFF ===== section; a real payload continues
    # with the full text of the touched file, so the stress prompt does exactly that:
    # payments.py as it reads after the change (the defective refund() at the top),
    # followed by the rest of the "module".
    changed = "\n".join(line[1:] for line in PROBE_DIFF.splitlines()
                        if line.startswith("+") and not line.startswith("+++"))
    return PROMPT + f"\n===== FULL FILE: payments.py =====\n{changed}\n{''.join(filler)}"

SEV_RE = re.compile(r"^\s*SEVERITY\s*:", re.M | re.I)
DONE_RE = re.compile(r"AUDIT COMPLETE", re.I)

# Below this a maximal payload (max_payload chars ~= a quarter of it in tokens) cannot fit
# in the seat's context and would truncate at the model, where nothing prints a warning.
MIN_CTX_TOKENS = 128_000


def probe_seat(provider: BaseProvider, seat: Seat, max_tokens: int,
               prompt: str = PROMPT) -> dict:
    """Send one review-shaped prompt and score the reply on findings emitted."""
    out = provider.call(seat.model, [{"role": "user", "content": prompt}],
                        max_tokens=max_tokens, temperature=1.0, timeout=PROBE_TIMEOUT)
    row = {"seat": seat.name, "model": seat.model, "family": seat.family,
           "provider": seat.provider, "seconds": out.seconds,
           "content_chars": len(out.content), "reasoning_chars": len(out.reasoning),
           "finish_reason": out.finish_reason}
    if out.error:
        return {**row, "verdict": "fail", "why": out.error, "findings": 0, "complete": False}
    findings = len(SEV_RE.findall(out.content))
    complete = bool(DONE_RE.search(out.content))
    if not out.content.strip():
        verdict, why = "fail", (f"empty content ({len(out.reasoning)} chars of reasoning, "
                                f"{out.finish_reason})")
    elif not complete:
        # Findings without the terminator means the reply was cut off. A truncated review
        # is a partial review, never a clean one.
        verdict, why = "thin", (f"{findings} findings but no terminator "
                                f"(truncated, {out.finish_reason})")
    elif findings >= EXPECT_FINDINGS:
        verdict, why = "good", f"{findings} findings, complete"
    else:
        verdict, why = "thin", f"found {findings} of {EXPECT_FINDINGS} known defects"
    return {**row, "verdict": verdict, "why": why, "findings": findings, "complete": complete}


def rank(rows: list[dict]) -> list[dict]:
    """Rank usable seats: context sufficiency, then findings, then least reasoning waste.

    Context comes first because it is a hard wall, not a preference — but UNKNOWN is not
    the same as NARROW. ctx_tokens is None when the provider's model listing doesn't
    advertise it, which is a property of the endpoint, not of the model; penalising it
    would let a silent listing demote a working family. A false demotion silently removes
    a good seat, while an over-generous one merely promotes a model that might truncate a
    maximal payload.

    Reasoning volume ranks next because it is a real risk: reasoning scales with input,
    and the probe diff is five lines. A model that needs tens of thousands of characters
    of reasoning on five lines is the one that returns an empty report on a real payload.
    """
    def key(r: dict) -> tuple:
        ctx = r.get("ctx_tokens")
        narrow = 1 if (ctx is not None and ctx < MIN_CTX_TOKENS) else 0
        # Stress health ranks right after the hard walls: a seat that stayed good at
        # stress size beat one that went thin there ON THE DIMENSION REAL WORK EXERCISES,
        # and a rank that ignored the stress record made the measurement decorative
        # (audit finding, 2026-09-02). Absent stress data (stage skipped) is neutral,
        # same reasoning as unknown ctx: unmeasured is not the same as bad.
        stress_v = (r.get("stress") or {}).get("verdict")
        stressed = 0 if stress_v in (None, "good") else 1
        return (narrow,
                stressed,
                -r.get("findings", 0),
                r.get("reasoning_chars", 10**9),
                -(ctx or 0))
    return sorted([r for r in rows if r["verdict"] == "good"], key=key)


def choose_panel(ranked: list[dict]) -> list[dict]:
    """One seat per family, best first — mirrors pick_panel's dedupe."""
    seen: set[str] = set()
    panel = []
    for r in ranked:
        if r["family"] in seen:
            continue
        seen.add(r["family"])
        panel.append(r)
    return panel


def run_probe(config: Config, json_out: bool = False) -> int:
    """Probe every configured seat, write roster.json atomically, print a summary.

    Exit codes: 0 healthy; 1 roster written but degraded (<2 usable families); 2 nothing
    probed at all (no seats, or no provider could even be constructed).
    """
    if not config.seats:
        print("ERROR: no seats configured — write a panel.toml first "
              "(see panel.example.toml)", file=sys.stderr)
        return 2

    providers: dict[str, BaseProvider] = {}
    dead_providers: dict[str, str] = {}
    for name in {s.provider for s in config.seats}:
        try:
            providers[name] = make_provider(config.providers[name])
        except ConfigError as e:
            # One provider's missing key must not block probing the others, but it must
            # be LOUD — its seats are recorded as fail, never silently absent.
            dead_providers[name] = str(e)
            print(f"WARNING: {e} — its seats will score fail", file=sys.stderr)

    print(f">> probing {len(config.seats)} configured seat(s)", file=sys.stderr)

    def one(seat: Seat) -> dict:
        if seat.provider in dead_providers:
            return {"seat": seat.name, "model": seat.model, "family": seat.family,
                    "provider": seat.provider, "verdict": "fail",
                    "why": dead_providers[seat.provider], "findings": 0,
                    "complete": False, "seconds": 0.0,
                    "content_chars": 0, "reasoning_chars": 0, "finish_reason": ""}
        return probe_seat(providers[seat.provider], seat, config.max_tokens)

    with cf.ThreadPoolExecutor(max_workers=min(6, len(config.seats))) as ex:
        rows = list(ex.map(one, config.seats))

    # One sample at temperature 1 is noisy: seats flip between thin and good on identical
    # input. Re-probe ONLY the non-good models and keep the better result. The asymmetry
    # is deliberate — a false thin/fail silently removes a working family from every later
    # panel, while a false good merely promotes a model that mostly works, and a single
    # seat's finding is already treated as a lead rather than a fact.
    seat_by_name = {s.name: s for s in config.seats}
    retry = [i for i, r in enumerate(rows)
             if r["verdict"] != "good" and r["provider"] not in dead_providers]
    if retry:
        print(f">> re-probing {len(retry)} non-good: "
              f"{', '.join(rows[i]['seat'] for i in retry)}", file=sys.stderr)
        with cf.ThreadPoolExecutor(max_workers=min(6, len(retry))) as ex:
            second = list(ex.map(lambda i: one(seat_by_name[rows[i]['seat']]), retry))
        order = {"good": 0, "thin": 1, "fail": 2}
        for i, alt in zip(retry, second):
            if order[alt["verdict"]] < order[rows[i]["verdict"]]:
                alt["why"] += " (on re-probe; first attempt: " + rows[i]["why"] + ")"
                rows[i] = alt
            else:
                rows[i]["attempts"] = 2

    # Stress stage: every seat that passed the small probe gets the same two defects at
    # realistic payload size. Only the SILENT failure demotes — empty content or a
    # transport death is the shape that kills real reviews while reading as clean; a seat
    # that answers but scores thin at stress size is degraded, not dead, and stays
    # eligible (its stress record is in the roster for anyone deciding to drop it).
    # A stress failure is re-probed once, same asymmetry as above: a false demotion
    # silently removes a working family from every later panel.
    good_idx = [i for i, r in enumerate(rows) if r["verdict"] == "good"]
    if good_idx:
        stress_prompt = build_stress_prompt()
        print(f">> stress-probing {len(good_idx)} good seat(s) at "
              f"{len(stress_prompt):,} chars", file=sys.stderr)

        def stress_one(i: int) -> dict:
            seat = seat_by_name[rows[i]["seat"]]
            return probe_seat(providers[seat.provider], seat, config.max_tokens,
                              prompt=stress_prompt)

        with cf.ThreadPoolExecutor(max_workers=min(6, len(good_idx))) as ex:
            stress = dict(zip(good_idx, ex.map(stress_one, good_idx)))
        sretry = [i for i in good_idx if stress[i]["verdict"] == "fail"]
        if sretry:
            print(f">> re-stress-probing {len(sretry)}: "
                  f"{', '.join(rows[i]['seat'] for i in sretry)}", file=sys.stderr)
            with cf.ThreadPoolExecutor(max_workers=min(6, len(sretry))) as ex:
                for i, alt in zip(sretry, ex.map(stress_one, sretry)):
                    if alt["verdict"] != "fail":
                        stress[i] = alt
        for i in good_idx:
            s = stress[i]
            rows[i]["stress"] = {k: s[k] for k in
                                 ("verdict", "why", "findings", "complete", "seconds",
                                  "content_chars", "reasoning_chars", "finish_reason")}
            if s["verdict"] == "fail":
                rows[i]["verdict"] = "thin"
                rows[i]["why"] += (f" | DEMOTED by stress probe: {s['why']} at "
                                   f"{len(stress_prompt):,} chars")

    # Context lengths, where the provider's listing advertises them (best effort, one GET
    # per provider). Unknown stays None — see rank().
    ctx_by_provider = {name: p.context_lengths() for name, p in providers.items()}
    for r in rows:
        r["ctx_tokens"] = ctx_by_provider.get(r["provider"], {}).get(r["model"])

    ranked = rank(rows)
    panel = choose_panel(ranked)
    roster = {
        "generated": int(time.time()),
        "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "expect_findings": EXPECT_FINDINGS,
        "stress_target_chars": STRESS_TARGET_CHARS,
        "max_tokens": config.max_tokens,
        # What the panel consumes: ranked, family-deduped, good-only seat names.
        "seats": [[r["seat"], r["family"]] for r in panel],
        "seat_detail": panel,
        "all": sorted(rows, key=lambda r: (r["verdict"], r["seat"])),
    }

    if len(panel) < 2:
        print(f"WARNING: only {len(panel)} usable famil{'y' if len(panel) == 1 else 'ies'} "
              f"— the default 2-seat panel cannot be filled, and --coder exclusion will "
              f"empty it", file=sys.stderr)

    out_path = config.roster_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(roster, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out_path)                       # atomic: a reader never sees half a roster

    if json_out:
        print(json.dumps(roster, indent=2))
    else:
        print(f"{'seat':<14}{'model':<38}{'verdict':<9}why", file=sys.stderr)
        for r in roster["all"]:
            print(f"{r['seat']:<14}{r['model']:<38}{r['verdict']:<9}{r.get('why', '')}"[:150],
                  file=sys.stderr)
        print(f"\n>> panel ({len(panel)} families): "
              f"{', '.join(r['seat'] for r in panel)}", file=sys.stderr)
        print(f">> written to {out_path}", file=sys.stderr)

    return 1 if len(panel) < 2 else 0
