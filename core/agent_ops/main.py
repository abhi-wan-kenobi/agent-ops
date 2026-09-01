"""CLI: `python3 -m agent_ops <repo> [...]` (review), plus `probe` and `runs`.

The review flow, in order: build the scoped payload → secret gate → pick the panel
(coder-family excluded) → claim a run dir and record the run → take the lease
(cooperatively, cancellable) → run every seat concurrently over plain HTTP → classify →
report, with loud warnings whenever fewer than two seats actually reported.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import pathlib
import re
import signal
import sys
import time

from . import run_state
from .classify import SECRET_RE, classify_seat
from .config import Config, ConfigError, Seat, load_config
from .init_cmd import run_init
from .lease import Lease
from .panel import family_of, load_probe_seconds, load_seats, pick_panel
from .payload import SEAT_NOTE_CHARS, build_payload
from .probe import run_probe
from .providers import BaseProvider, make_provider
from .report import PROMPT_HEAD, append_stats, write_payload, write_seat_report
from .stats import run_stats, run_verdict

DEFAULT_TIMEOUT = 900
# Probe-informed cap: ~6x the seat's measured probe latency, floored so a fast probe
# never strangles a real review (payloads are far bigger than the probe diff), and never
# ABOVE the default budget — this exists to fail dead-slow seats in minutes, not to grant
# extensions. An explicit --timeout overrides all of it.
TIMEOUT_MULTIPLIER = 6
TIMEOUT_FLOOR = 120


def seat_timeouts(panel: list[Seat], probed: dict[str, float],
                  explicit: int | None) -> dict[str, int]:
    """Per-seat timeout in seconds, keyed by seat name."""
    if explicit is not None:
        return {s.name: explicit for s in panel}
    out: dict[str, int] = {}
    for s in panel:
        secs = probed.get(s.name)
        if secs is None:
            out[s.name] = DEFAULT_TIMEOUT
        else:
            out[s.name] = min(DEFAULT_TIMEOUT,
                              max(TIMEOUT_FLOOR, int(secs * TIMEOUT_MULTIPLIER)))
    return out


def claim_run_dir(root: pathlib.Path, attempts: int = 50) -> tuple[str, pathlib.Path]:
    """Atomically claim a unique (run_id, output dir).

    A bare timestamp id collides: two runs started in the same second overwrite each
    other's run record and report files — real reports destroyed, not a cosmetic bug.
    `mkdir` is atomic on POSIX, so the first caller to create <stamp> owns it and everyone
    else falls through to <stamp>-2, -3, ... Claiming the DIRECTORY is what makes the ID
    unique, so the two can never disagree.
    """
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(1, attempts + 1):
        rid = stamp if n == 1 else f"{stamp}-{n}"
        d = root / rid
        try:
            d.mkdir()
            return rid, d
        except FileExistsError:
            continue
    raise RuntimeError(f"could not claim a unique run dir under {root} for {stamp}")


def run_seat(provider: BaseProvider, seat: Seat, prompt: str, outdir: pathlib.Path,
             timeout: int, max_tokens: int) -> dict:
    out = provider.call(seat.model, [{"role": "user", "content": prompt}],
                        max_tokens=max_tokens, timeout=timeout)
    status, findings, reason = classify_seat(
        out.content, timed_out=(out.error == "timeout"),
        failed=bool(out.error and out.error != "timeout"), reason=out.error or "")
    write_seat_report(outdir, seat.family, seat.model, status, reason, out.content)
    return {"model": seat.model, "seat": seat.name, "family": seat.family,
            "findings": findings, "status": status, "reason": reason,
            "truncated": status == "truncated", "seconds": out.seconds,
            "chars": len(out.content)}


def acquire_lease_cooperatively(lease: Lease, run_id: str, label: str, ttl: int = 3600,
                                waitfor: float = 3600, poll: float = 1.0) -> tuple[bool, bool]:
    """Poll the lease ourselves instead of blocking inside it, so a run waiting behind
    another panel notices a cancel request between attempts.

    Returns (held, cancelled); never both true.
    """
    deadline = time.monotonic() + waitfor
    run_state.update_run(run_id, lease={"held": False, "waiting_since": time.time(),
                                        "held_since": None})
    while True:
        if run_state.cancel_requested(run_id):
            return False, True
        if lease.try_acquire(ttl=ttl, label=label, owner_pid=os.getpid()) is not None:
            return True, False
        if time.monotonic() >= deadline:
            return False, False
        time.sleep(poll)


def run_panel_cooperatively(run_id: str, panel: list[Seat], seat_runner,
                            poll: float = 1.0) -> tuple[list[dict], bool]:
    """Run every seat concurrently, checking for a cooperative cancel between completions.

    Abandons rather than kills: an in-flight HTTP call runs to its own timeout even after
    this stops waiting on it. `ex.shutdown(wait=False, cancel_futures=True)` is what lets
    this return without joining those threads — but ONLY outside a `with` block: a
    context-managed executor's __exit__ calls shutdown(wait=True) again on the way out,
    silently turning "abandon" back into "wait for it anyway".
    """
    def safe_runner(seat: Seat) -> dict:
        # A crashed seat (report write failing, a provider bug) must become an honest
        # `error` seat, not kill the whole panel: an exception escaping here would leave
        # the run record permanently "running" and take the other seats down with it.
        # Audit finding 2026-09-01, flagged independently by two families.
        try:
            return seat_runner(seat)
        except Exception as e:                                # noqa: BLE001
            return {"model": seat.model, "seat": seat.name, "family": seat.family,
                    "findings": None, "status": "error",
                    "reason": f"seat crashed: {type(e).__name__}: {e}",
                    "truncated": False, "seconds": 0.0, "chars": 0}

    ex = cf.ThreadPoolExecutor(max_workers=len(panel))
    futures = {ex.submit(safe_runner, s): s for s in panel}
    pending = set(futures)
    results: list[dict] = []
    while pending:
        done, pending = cf.wait(pending, timeout=poll, return_when=cf.FIRST_COMPLETED)
        for fut in done:
            r = fut.result()
            run_state.update_seat(run_id, r["model"], status=r["status"],
                                  findings=r["findings"], seconds=r["seconds"])
            results.append(r)
        if pending and run_state.cancel_requested(run_id):
            for fut in pending:
                run_state.update_seat(run_id, futures[fut].model, status="abandoned")
            ex.shutdown(wait=False, cancel_futures=True)
            return results, True
    ex.shutdown(wait=False)
    return results, False


def _subdir_name(index: int, path: str) -> str:
    """Filesystem-safe per-file report dir. The index prefix keeps names unique even when
    sanitising collapses two paths to the same string."""
    return f"{index:02d}-" + re.sub(r"[^A-Za-z0-9._-]", "__", path)[:80]


def _audit_split(a, config: Config, repo: pathlib.Path, run_id: str,
                 outdir: pathlib.Path, files: list[str], panel: list[Seat],
                 providers: dict[str, BaseProvider], timeouts: dict[str, int],
                 focus: str) -> int:
    """--split-by-file: one panel per changed file, sequentially, under the ONE lease the
    caller already holds. Replaces the hand-written shell loop the v0.1 build needed.

    Sequential on purpose: the lease exists because concurrency against a rate-limited
    endpoint queues silently until seats blow their budgets, and running N files' panels
    at once is that same failure with extra steps.

    Each file gets its own subdir (reports + the exact PAYLOAD.txt that left) and its own
    stats line under `<run-id>/<subdir>`, because verdicts must land on the per-file
    report a human actually read — an aggregate line would make finding numbers ambiguous
    across files.
    """
    per_file: list[dict] = []                 # {"file", "sub", "results"|None, "why"}
    cancelled = False
    for i, f in enumerate(files, 1):
        if run_state.cancel_requested(run_id):
            cancelled = True
            break
        payload, _, desc = build_payload(repo, a.scope, f, config.max_payload, exact=True)
        if not payload.strip():
            print(f">> [{i}/{len(files)}] {f}: produced no payload — SKIPPED "
                  f"(changed since discovery?)", file=sys.stderr)
            per_file.append({"file": f, "sub": None, "results": None, "why": "empty"})
            continue
        # The whole-scope gate already ran, but it scanned a payload that may have been
        # TRUNCATED at max_payload — a secret past the cut would sail through. Gate the
        # exact per-file text that is about to leave; skip that file, keep reviewing.
        if SECRET_RE.search(payload):
            print(f">> [{i}/{len(files)}] {f}: ⛔ SKIPPED — looks like it contains a live "
                  f"credential. Remove it and re-run this file.", file=sys.stderr)
            per_file.append({"file": f, "sub": None, "results": None, "why": "secret"})
            continue
        sub = _subdir_name(i, f)
        subdir = outdir / sub
        subdir.mkdir(parents=True, exist_ok=True)
        write_payload(subdir, payload)
        prompt = f"{PROMPT_HEAD}{focus}\nSCOPE: {desc} in {repo.name}\n\n{payload}"
        print(f">> [{i}/{len(files)}] {f} ({len(payload):,} chars)", file=sys.stderr)
        results, cancelled = run_panel_cooperatively(
            run_id, panel,
            lambda s: run_seat(providers[s.provider], s, prompt, subdir,
                               timeouts[s.name], config.max_tokens))
        for r in results:
            if r["findings"] is None:
                print(f"   {r['family']:10} ⛔ DID NOT RUN — {r['reason']} "
                      f"({r['seconds']}s)", file=sys.stderr)
            else:
                flag = "  ⚠️ TRUNCATED" if r["truncated"] else ""
                print(f"   {r['family']:10} {r['findings']} findings  "
                      f"({r['seconds']}s){flag}", file=sys.stderr)
        append_stats(config.stats_path, run_id=f"{run_id}/{sub}", repo_name=repo.name,
                     scope=desc, files=1, payload_chars=len(payload), coder=a.coder,
                     seats=results, parent=run_id)
        per_file.append({"file": f, "sub": sub, "results": results, "why": None})
        if cancelled:
            break

    if cancelled:
        note = (run_state.read_run(run_id) or {}).get("note")
        run_state.finish_run(run_id, "cancelled", note=note)
        print(f"\n>> CANCELLED after {len(per_file)} of {len(files)} files — later files "
              f"were never reviewed.", file=sys.stderr)
        return 8

    # One summary for the whole run — the point of the flag. Per-file quorum is judged
    # the same way as a single run's: a file where <2 seats reported is a lead-quality
    # review of THAT file, and a file where none reported was not reviewed at all.
    reviewed = [pf for pf in per_file if pf["results"] is not None]
    skipped = [pf for pf in per_file if pf["results"] is None]
    unreviewed = [pf for pf in reviewed
                  if not any(r["findings"] is not None for r in pf["results"])]
    thin = [pf for pf in reviewed
            if 0 < len([r for r in pf["results"] if r["findings"] is not None]) < 2]
    print(f"\n>> summary  : {len(reviewed)} file(s) reviewed"
          + (f", {len(skipped)} skipped" if skipped else ""), file=sys.stderr)
    for pf in reviewed:
        counts = ", ".join(
            f"{r['family']} {'—' if r['findings'] is None else r['findings']}"
            for r in pf["results"])
        print(f"   {pf['file']}: {counts}   (verdicts: {run_id}/{pf['sub']})",
              file=sys.stderr)
    for pf in skipped:
        print(f"   {pf['file']}: SKIPPED ({pf['why']}) — NOT reviewed", file=sys.stderr)
    if unreviewed:
        print(f"\n>> ⛔ {len(unreviewed)} file(s) got NO report at all — those files were "
              f"not reviewed. Not clean.", file=sys.stderr)
    if thin:
        print(f">> ⚠️ {len(thin)} file(s) had only one reporting seat — leads, not "
              f"agreement. Re-run the missing seats before treating them as done.",
              file=sys.stderr)
    print(f"\n>> reports in {outdir}/ (one subdir per file)", file=sys.stderr)
    print(">> NEXT: read each file's reports and cross-reference; verify every finding\n"
          "   against the real code, then close the loop per finding:\n"
          f"   python3 -m agent_ops verdict {run_id}/<subdir> <family> <n> confirmed|fp",
          file=sys.stderr)

    ok = bool(reviewed) and not skipped and not unreviewed
    run_state.finish_run(run_id, "done" if ok else "failed", exit_code=0 if ok else 1,
                         error=None if ok else "not every file was fully reviewed")
    return 0 if ok else 1


def _make_cancel_signal_handler(run_id: str):
    """A killed run must not leave a permanently "running"/"queued" record.

    The handler writes the terminal record itself and re-raises as SystemExit so the
    exception still unwinds through main()'s try/finally, which is what releases the
    lease exactly as a normal exit would.
    """
    def handler(signum, frame):                                # noqa: ANN001
        run_state.finish_run(run_id, "cancelled",
                             error=f"terminated by signal {signum}")
        raise SystemExit(130)
    return handler


def audit(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="agent_ops", description="Adversarial multi-model review panel")
    ap.add_argument("repo")
    ap.add_argument("--scope", default="uncommitted",
                    help="uncommitted (default) | last | commit:<ref> | <git-ref>")
    ap.add_argument("--coder", help="model that WROTE the code; its family is excluded")
    ap.add_argument("--models", help="explicit comma-separated panel by seat name or model "
                                     "id (overrides rotation AND coder exclusion)")
    ap.add_argument("--seats", type=int, default=2)
    ap.add_argument("--focus")
    ap.add_argument("--timeout", type=int, default=None,
                    help=f"per-seat wall clock in seconds (default: {DEFAULT_TIMEOUT}, or "
                         f"~{TIMEOUT_MULTIPLIER}x the seat's probed latency when a fresh "
                         f"roster has one)")
    ap.add_argument("--only", help="review only files whose path contains this substring "
                                   "(use it — split large changes)")
    ap.add_argument("--split-by-file", action="store_true",
                    help="one panel per changed file, sequentially, under one lease — "
                         "the multi-file discipline as a single command instead of a "
                         "hand-written loop (combines with --only, which narrows first)")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--config", help="panel.toml path (default ~/.agent-ops/panel.toml)")
    a = ap.parse_args(argv)

    try:
        config = load_config(a.config)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2
    run_state.RUNS_DIR = config.runs_dir

    repo = pathlib.Path(a.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"ERROR: {repo} is not a directory", file=sys.stderr)
        return 2

    payload, files, desc = build_payload(repo, a.scope, a.only, config.max_payload)
    if not payload.strip() or not files:
        print(f"NOTE: '{a.scope}' produced no diff in {repo} — nothing to review.",
              file=sys.stderr)
        return 1

    # This is the exact text that leaves the machine, so scan THAT — a gate that scans the
    # diff alone while the payload carries whole files is a documented hole.
    if SECRET_RE.search(payload):
        print("REFUSED: the payload contains what looks like a live credential "
              "(JWT / OAuth secret / token / private key).", file=sys.stderr)
        print("         Remove it or narrow --scope, then re-run.", file=sys.stderr)
        return 3

    if len(payload) > SEAT_NOTE_CHARS:
        print(f"note: payload is {len(payload):,} chars. If the reports come back empty, "
              f"split it with --only <path> before concluding the code is clean.",
              file=sys.stderr)

    seats, provenance = load_seats(config)
    if not seats:
        print("ERROR: no seats configured — write a panel.toml first "
              "(see panel.example.toml in the plugin).", file=sys.stderr)
        return 2
    if not a.coder and not a.models:
        print(">> ⚠️ no --coder given. Family rotation only works when the panel knows "
              "which family wrote the code — pass --coder <model>.", file=sys.stderr)
    override = a.models.split(",") if a.models else None
    panel = pick_panel(a.coder, a.seats, override, seats)
    if override:
        # A typo in --models must not silently shrink the panel.
        resolved = {s.name for s in panel} | {s.model for s in panel}
        for w in override:
            if w.strip() and w.strip() not in resolved:
                print(f">> ⚠️ --models: {w.strip()!r} matches no configured seat name or "
                      f"model id — ignored.", file=sys.stderr)
    if not panel:
        print("ERROR: no eligible seats (does the config have at least two families, "
              "excluding the coder's?)", file=sys.stderr)
        return 2

    try:
        providers = {name: make_provider(config.providers[name])
                     for name in {s.provider for s in panel}}
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    # Preflight, best effort per provider: a reachable listing that lacks the model turns
    # a slow per-seat failure into one line now. An unreachable listing proves nothing and
    # must not block — GET /models being down does not mean POST /chat/completions is.
    known: dict[str, set[str] | None] = {name: p.model_ids() for name, p in providers.items()}
    unroutable = [s for s in panel
                  if known[s.provider] is not None and s.model not in known[s.provider]]
    for s in unroutable:
        print(f">> {s.name:14} ⛔ NOT ROUTABLE — provider {s.provider!r} does not list "
              f"model {s.model!r}. Fix the model id in panel.toml or pick another seat.",
              file=sys.stderr)
    panel = [s for s in panel if s not in unroutable]
    if not panel:
        print("\n>> ⛔ NO ROUTABLE SEAT — this review did not happen. Not clean.",
              file=sys.stderr)
        return 2

    timeouts = seat_timeouts(panel, load_probe_seconds(config), a.timeout)

    # The run record is created BEFORE the lease is requested, so a run waiting behind
    # another panel is visible as "queued" rather than not existing yet.
    run_id, outdir = claim_run_dir(config.outroot)
    label = f"agent-ops:{os.getpid()}"
    run_state.new_run(run_id, str(outdir), str(repo), a.scope, a.coder)

    old_handlers = [(sig, signal.signal(sig, _make_cancel_signal_handler(run_id)))
                    for sig in (signal.SIGINT, signal.SIGTERM)]

    lease = Lease(config.lease_dir, max_slots=config.lease_slots)
    held = False
    try:
        if not a.no_lock:
            print(f">> lease    : up to {config.lease_slots} concurrent run(s)",
                  file=sys.stderr)
            held, cancelled = acquire_lease_cooperatively(lease, run_id, label)
            if cancelled:
                note = (run_state.read_run(run_id) or {}).get("note")
                run_state.finish_run(run_id, "cancelled", note=note)
                print("CANCELLED: run was cancelled while waiting for the panel lease",
                      file=sys.stderr)
                return 8
            if not held:
                run_state.finish_run(run_id, "failed",
                                     error="could not acquire the panel lease")
                print("ERROR: another review holds the lease", file=sys.stderr)
                return 7

        run_state.update_run(run_id, state="running", started_at=time.time(),
                             lease={"held": held, "waiting_since": None,
                                    "held_since": time.time() if held else None})

        focus = f"\nPARTICULAR FOCUS: {a.focus}\n" if a.focus else ""
        prompt = f"{PROMPT_HEAD}{focus}\nSCOPE: {desc} in {repo.name}\n\n{payload}"

        print(f">> reviewing: {repo}", file=sys.stderr)
        print(f">> scope    : {desc} ({len(files)} files, {len(payload):,} chars)",
              file=sys.stderr)
        print(f">> coder    : {a.coder or '(unspecified)'}", file=sys.stderr)
        print(f">> panel    : {', '.join(f'{s.name} ({s.model})' for s in panel)}",
              file=sys.stderr)
        # Name each cap out loud: a seat killed at 4 minutes must be explainable from the
        # run's own output, not from reading the roster and doing the arithmetic by hand.
        print(f">> timeouts : "
              + ", ".join(f"{s.name} {timeouts[s.name]}s"
                          + (" (probed)" if a.timeout is None
                             and timeouts[s.name] != DEFAULT_TIMEOUT else "")
                          for s in panel), file=sys.stderr)
        # Say where the panel came from. A silent fallback is exactly how a stale roster
        # stays invisible. --models bypasses the roster, so naming it there would be a lie.
        print(f">> seats from: {'--models (explicit)' if a.models else provenance}",
              file=sys.stderr)
        print(f">> run id   : {run_id} (cancel: python3 -m agent_ops runs cancel {run_id})",
              file=sys.stderr)

        run_state.set_panel(run_id, [(s.model, s.family) for s in panel])

        if a.split_by_file:
            print(f">> split    : one panel per file ({len(files)} files, sequential)",
                  file=sys.stderr)
            return _audit_split(a, config, repo, run_id, outdir, files, panel,
                                providers, timeouts, focus)

        write_payload(outdir, payload)
        results, cancelled = run_panel_cooperatively(
            run_id, panel,
            lambda s: run_seat(providers[s.provider], s, prompt, outdir,
                               timeouts[s.name], config.max_tokens))
        if cancelled:
            note = (run_state.read_run(run_id) or {}).get("note")
            run_state.finish_run(run_id, "cancelled", note=note)
            print("\n>> CANCELLED: run was cancelled while seats were in flight — "
                  "abandoned whatever had not yet reported.", file=sys.stderr)
            return 8

        for r in results:
            if r["findings"] is None:
                print(f">> {r['family']:10} ⛔ DID NOT RUN — {r['reason']}  "
                      f"({r['seconds']}s)", file=sys.stderr)
                continue
            flag = "  ⚠️ TRUNCATED (count inferred)" if r["truncated"] else ""
            print(f">> {r['family']:10} {r['findings']} findings  ({r['seconds']}s){flag}",
                  file=sys.stderr)

        # The whole confidence model is "2+ seats agreeing". If the panel did not actually
        # convene, say so here rather than letting the reader infer agreement from silence.
        reported = [r for r in results if r["findings"] is not None]
        if not reported:
            print("\n>> ⛔ NO SEAT PRODUCED A REPORT — this review did not happen. "
                  "Not clean.", file=sys.stderr)
        elif len(reported) < 2:
            print(f"\n>> ⚠️ ONLY {len(reported)} of {len(results)} seats reported. A single "
                  "seat is a LEAD,\n   not a fact, and a single clean seat is not a cleared "
                  "change. Re-run the\n   missing seats before treating this as done.",
                  file=sys.stderr)

        append_stats(config.stats_path, run_id=run_id, repo_name=repo.name, scope=desc,
                     files=len(files), payload_chars=len(payload), coder=a.coder,
                     seats=results)

        print(f"\n>> reports in {outdir}/", file=sys.stderr)
        print(">> NEXT: read each report and cross-reference. 2+ seats agreeing = high",
              file=sys.stderr)
        print("   confidence. A single-seat finding is a LEAD — verify it against the real",
              file=sys.stderr)
        print("   code before acting, and discard it if it does not hold up.",
              file=sys.stderr)
        # Exit non-zero when nothing was reviewed, so a scripted caller cannot record a
        # dead panel as a passed gate.
        rc = 0 if reported else 1
        run_state.finish_run(run_id, "done" if rc == 0 else "failed", exit_code=rc,
                             error=None if rc == 0 else "no seat produced a report")
        return rc
    finally:
        for sig, old in old_handlers:
            signal.signal(sig, old)
        if held:
            lease.release(label)


def _pop_config(argv: list[str]) -> tuple[str | None, list[str]]:
    """Extract --config from a subcommand's argv (probe/runs share the flag)."""
    out: list[str] = []
    cfg: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--config" and i + 1 < len(argv):
            cfg = argv[i + 1]
            i += 2
        elif argv[i].startswith("--config="):
            cfg = argv[i].split("=", 1)[1]
            i += 1
        else:
            out.append(argv[i])
            i += 1
    return cfg, out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd == "init":
        return run_init(argv[1:])
    if cmd in ("verdict", "stats"):
        cfg_path, rest = _pop_config(argv[1:])
        try:
            stats_path = load_config(cfg_path).stats_path
        except ConfigError as e:
            print(f"CONFIG ERROR: {e}", file=sys.stderr)
            return 2
        return (run_verdict if cmd == "verdict" else run_stats)(stats_path, rest)
    if cmd == "probe":
        cfg_path, rest = _pop_config(argv[1:])
        json_out = "--json" in rest
        try:
            return run_probe(load_config(cfg_path), json_out=json_out)
        except ConfigError as e:
            print(f"CONFIG ERROR: {e}", file=sys.stderr)
            return 2
    if cmd == "runs":
        cfg_path, rest = _pop_config(argv[1:])
        try:
            run_state.RUNS_DIR = load_config(cfg_path).runs_dir
        except ConfigError as e:
            print(f"CONFIG ERROR: {e}", file=sys.stderr)
            return 2
        return run_state.cli(rest)
    if cmd == "audit":
        argv = argv[1:]
    return audit(argv)
