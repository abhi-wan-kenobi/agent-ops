"""Durable run-state store for the review panel.

WHY THIS EXISTS. The lease serialises concurrent runs so a saturated endpoint doesn't
inflate every panel's wall-clock, but a lease alone gives no way to SEE that a run is
queued behind it, how long it has waited, or who started it — a blocked run and a slow one
look identical from outside, and nobody can tell a UI "cancel that one". This module is
the record an outside reader consumes; it never itself decides to run or skip a review.

One JSON file per run under RUNS_DIR, written atomically (temp file + os.replace) so a
reader never sees a half-written record. State you cannot trust atomically is worse than
no state.

STALENESS. A run stuck in queued/running whose owning PID is gone did not finish quietly,
it died (OOM, kill -9, host reboot). gc_runs() verifies liveness with os.kill(pid, 0) —
plus the pid's start time where /proc exists — rather than trusting a timestamp, and is
folded into list_runs() so nothing that calls it has to remember to GC first.
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
from typing import Any

try:
    import fcntl
except ImportError:                                            # non-POSIX: in-process lock only
    fcntl = None                                               # type: ignore[assignment]

# Overridden by main() from config.state_dir before any run is recorded.
RUNS_DIR = pathlib.Path("~/.agent-ops/state/runs").expanduser()

TERMINAL_STATES = {"done", "failed", "cancelled"}
ACTIVE_STATES = {"queued", "running"}

# Terminal records are pure history once finished; without a cutoff they accumulate
# forever. 14 days matches ROSTER_MAX_AGE_DAYS's reasoning in panel.py.
TERMINAL_RETENTION_DAYS = 14


def _now() -> float:
    return time.time()


def _path(run_id: str) -> pathlib.Path:
    return RUNS_DIR / f"{run_id}.json"


# The panel runs seats CONCURRENTLY IN THREADS, so every writer below is contending with
# its siblings inside one process as well as with an external canceller in another.
#
# A PID-suffixed temp name is not enough and failed in test: two threads in the same
# process derive the SAME temp path, the first os.replace() renames it away, and the
# second raises FileNotFoundError mid-run. mkstemp is unique per call, per thread.
_WRITE_LOCK = threading.Lock()
# finish_run() legitimately writes a terminal state over a terminal state (SIGTERM
# arriving while main()'s finally block is already finishing). It sets this flag.
_FORCE = contextvars.ContextVar("run_state_force_write", default=False)


@contextlib.contextmanager
def _record_lock(path: pathlib.Path):
    """Serialise a read-modify-write against other threads AND other processes.

    Without this, two seats landing together each read the record, each patch their own
    entry, and the second write drops the first — the tracker then under-reports what
    actually ran, which is worse than having no tracker. The lock file is a sidecar so it
    survives the os.replace() that swaps the record itself.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _WRITE_LOCK:
        with open(lock_path, "w") as fh:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)      # atomic on POSIX — never a half-written record
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_run(run_id: str) -> dict | None:
    try:
        return json.loads(_path(run_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        # Corrupt or mid-write: a caller only checking on a run's health must not crash.
        return None


def _session_identity() -> dict:
    """Whatever identifies the calling session, captured honestly — never invented."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    return {"session_id": session_id, "pid": os.getpid(),
            "pid_start_time": _pid_start_time(os.getpid())}


def new_run(run_id: str, outdir: str, repo: str, scope: str, coder: str | None) -> dict:
    """Create and persist the initial queued record — before the lease is even requested,
    so a run waiting behind another review is visible as "queued", not absent."""
    record: dict = {
        "run_id": run_id,
        "outdir": outdir,
        "state": "queued",
        "started_by": {**_session_identity(), "repo": repo, "scope": scope},
        "coder": coder,
        "queued_at": _now(),
        "started_at": None,
        "finished_at": None,
        "lease": {"held": False, "waiting_since": _now(), "held_since": None},
        "panel": [],
        "cancel_requested": False,
        "note": None,
        "exit_code": None,
        "error": None,
    }
    _atomic_write(_path(run_id), record)
    return record


def update_run(run_id: str, **fields: Any) -> dict | None:
    """Read-modify-atomic-write. Returns None if the record is gone (e.g. GC'd out from
    under a caller) rather than silently recreating a run that no longer exists."""
    with _record_lock(_path(run_id)):
        record = read_run(run_id)
        if record is None:
            return None
        # A terminal record is final. Cancellation abandons seats but does not stop the
        # threads already in flight, so a seat landing afterwards would otherwise patch —
        # and with a `state` field, resurrect — a run the human was already told is over.
        if record.get("state") in TERMINAL_STATES and not _FORCE.get():
            return record
        record.update(fields)
        _atomic_write(_path(run_id), record)
        return record


def set_panel(run_id: str, seats: list[tuple[str, str]]) -> None:
    update_run(run_id, panel=[
        {"model": m, "family": f, "status": "pending", "findings": None, "seconds": None}
        for m, f in seats])


def update_seat(run_id: str, model: str, **fields: Any) -> None:
    """Patch one seat's panel entry as its result lands — the whole point of this store: a
    reader sees per-seat progress instead of only a single result at the very end."""
    with _record_lock(_path(run_id)):
        record = read_run(run_id)
        if record is None:
            return
        if record.get("state") in TERMINAL_STATES:
            return                       # see update_run: terminal is final
        for seat in record.get("panel", []):
            if seat.get("model") == model:
                seat.update(fields)
                break
        _atomic_write(_path(run_id), record)


def cancel_requested(run_id: str) -> bool:
    record = read_run(run_id)
    return bool(record and record.get("cancel_requested"))


def request_cancel(run_id: str, note: str | None = None) -> dict | None:
    """What a human or a UI calls. Cooperative only: this just sets a flag the running
    panel polls on its own (while waiting for the lease, and between seats finishing) — it
    does not touch the process in any way.

    Returns None for an unknown run AND for one that has already finished, so a caller can
    tell "cancelled" from "too late" instead of reporting success either way. Cancelling a
    finished run is not an error, but claiming to have cancelled one is a lie.
    """
    record = read_run(run_id)
    if record is None or record.get("state") in TERMINAL_STATES:
        return None
    return update_run(run_id, cancel_requested=True, note=note)


def finish_run(run_id: str, state: str, **fields: Any) -> dict | None:
    """Terminal state, and the lease flag cleared with it.

    A finished run has released the lease — main()'s finally block does that — but leaving
    `lease.held: True` in the record would advertise a lock the lease reports free. A
    tracker misreporting exactly the thing it exists to show is worse than no tracker.
    `held_since` is kept as the historical fact of when it DID hold; `released_at` records
    the other end.
    """
    assert state in TERMINAL_STATES, f"not a terminal state: {state}"
    token = _FORCE.set(True)
    try:
        return _finish_locked(run_id, state, fields)
    finally:
        _FORCE.reset(token)


def _finish_locked(run_id: str, state: str, fields: dict) -> dict | None:
    with _record_lock(_path(run_id)):
        record = read_run(run_id)
        if record is None:
            return None
        lease = dict(record.get("lease") or {})
        if lease.get("held"):
            lease["held"] = False
            lease["released_at"] = _now()
        lease["waiting_since"] = None
        record.update(state=state, finished_at=_now(), lease=lease, **fields)
        _atomic_write(_path(run_id), record)
        return record


def _pid_start_time(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat: the process's start time in clock ticks since boot.

    A pid alone cannot identify a process — pids are reused, and a long-lived orphan
    record holding a recycled pid reads as alive forever. The pair (pid, start_time) is
    unique for the life of the boot. Linux-only best effort: on hosts without /proc this
    returns None and liveness falls back to os.kill(pid, 0) alone.
    """
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    # comm (field 2) may contain spaces and parens; everything after the LAST ')' is safe.
    try:
        return int(stat[stat.rindex(")") + 2:].split()[19])
    except (ValueError, IndexError):
        return None


def _pid_alive(pid: int | None, start_time: int | None = None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass                    # exists, just owned by someone else — still alive
    except OSError:
        return False
    if start_time is None:
        return True             # recorded before start_time was captured; pid is all we have
    current = _pid_start_time(pid)
    # Unreadable /proc (another user's process, or no /proc) leaves the pid check as the
    # only evidence.
    return True if current is None else current == start_time


def gc_runs() -> None:
    """Reap dead runs and prune old terminal history.

    A record left in queued/running with a dead owning PID is not evidence of a long
    review, it is evidence of a crash — verified against the live process table, not a
    timestamp. Runs already terminal are pruned purely by age once past
    TERMINAL_RETENTION_DAYS.
    """
    if not RUNS_DIR.is_dir():
        return
    now = _now()
    for p in RUNS_DIR.glob("*.json"):
        if p.name.endswith(".tmp"):
            continue
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        state = record.get("state")
        if state in ACTIVE_STATES:
            owner = record.get("started_by") or {}
            pid = owner.get("pid")
            if not _pid_alive(pid, owner.get("pid_start_time")):
                # Re-read under the record lock: without it GC races finish_run() and can
                # stamp "orphaned" over a run that terminated cleanly a moment earlier.
                with _record_lock(p):
                    fresh = read_run(p.stem)
                    if fresh is None or fresh.get("state") not in ACTIVE_STATES:
                        continue
                    fresh.update(state="failed", finished_at=now,
                                 error=f"orphaned: owning process {pid} no longer exists "
                                       f"(detected by GC)")
                    _atomic_write(p, fresh)
        elif state in TERMINAL_STATES:
            finished = record.get("finished_at") or record.get("queued_at") or 0
            if now - finished > TERMINAL_RETENTION_DAYS * 86400:
                for victim in (p, p.with_suffix(p.suffix + ".lock")):
                    try:
                        victim.unlink()
                    except OSError:
                        pass

    # Lock sidecars whose record is gone: one file per run, never removed, forever.
    for lock in RUNS_DIR.glob("*.json.lock"):
        if not lock.with_suffix("").exists():
            try:
                lock.unlink()
            except OSError:
                pass


def list_runs() -> list[dict]:
    """Read-only, GC'd listing. Import this (or shell out to `agent_ops runs list --json`)
    rather than re-parsing the run files — the GC and the schema live here."""
    gc_runs()
    out: list[dict] = []
    if not RUNS_DIR.is_dir():
        return out
    for p in sorted(RUNS_DIR.glob("*.json")):
        if p.name.endswith(".tmp"):
            continue
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        record["age_seconds"] = round(now_minus(record.get("queued_at")), 1)
        out.append(record)
    return out


def now_minus(ts: float | None) -> float:
    return _now() - ts if ts else 0.0


def _cli_list(as_json: bool) -> int:
    runs = list_runs()
    if as_json:
        print(json.dumps(runs, indent=2))
        return 0
    if not runs:
        print("no runs recorded")
        return 0
    for r in runs:
        lease = r.get("lease") or {}
        lease_flag = "HELD" if lease.get("held") else ("WAIT" if r["state"] == "queued" else "-")
        seats = r.get("panel") or []
        done = sum(1 for s in seats if s.get("status") not in (None, "pending"))
        session = (r.get("started_by") or {}).get("session_id") or "?"
        print(f"{r['run_id']:16} {r['state']:10} {lease_flag:5} "
              f"seats {done}/{len(seats)}  age {r['age_seconds']:>6.0f}s  "
              f"session={session}  {r.get('outdir', '')}")
    return 0


def _cli_show(run_id: str) -> int:
    record = read_run(run_id)
    if record is None:
        print(f"no such run: {run_id}", file=sys.stderr)
        return 1
    print(json.dumps(record, indent=2))
    return 0


def _cli_cancel(run_id: str, note: str | None) -> int:
    existing = read_run(run_id)
    record = request_cancel(run_id, note=note)
    if record is None:
        if existing is None:
            print(f"no such run: {run_id}", file=sys.stderr)
        else:
            print(f"{run_id} already finished ({existing.get('state')}) — nothing to cancel",
                  file=sys.stderr)
        return 1
    print(f"cancel requested for {run_id}" + (f" ({note})" if note else ""))
    return 0


def cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent_ops runs",
                                 description="Inspect or cancel panel run records")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list all known runs (GC'd first)")
    p_list.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="print one run's full record")
    p_show.add_argument("run_id")

    p_cancel = sub.add_parser("cancel", help="cooperatively request cancellation of a run")
    p_cancel.add_argument("run_id")
    p_cancel.add_argument("--note", default=None)

    a = ap.parse_args(argv)
    if a.cmd == "list":
        return _cli_list(a.json)
    if a.cmd == "show":
        return _cli_show(a.run_id)
    if a.cmd == "cancel":
        return _cli_cancel(a.run_id, a.note)
    return 2
