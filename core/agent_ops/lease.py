"""The panel concurrency lease — a counting semaphore made of directories.

WHY: a saturated endpoint QUEUES concurrent requests rather than rejecting them (measured
twice on real lanes: 6 concurrent requests ran at an 8-10x latency tail while 1-3 were
served cleanly). A panel makes several calls at once, so oversubscribing inflates every
seat's wall-clock until one blows its time budget and reports an incomplete result that
looks finding-free. Don't stack panels; take a lease.

The lease is an ATOMIC DIRECTORY PER SLOT plus an expiry, not a process:
  * `os.mkdir` is atomic on POSIX, so acquisition cannot race;
  * state is a file you can read, so status never has to be inferred;
  * every lease carries an EXPIRY, so a forgetful or dead holder stalls other runs for at
    most the TTL instead of wedging them forever;
  * an optional OWNER pid breaks the lease early if that process dies.

Default one slot. The shell original allowed a timed "boost" to widen the cap; that was a
feature of a multi-account gateway and is deliberately dropped — pass `max_slots` if your
provider is measured to serve more than one panel cleanly.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time

try:
    import fcntl
except ImportError:                                            # non-POSIX: no gate lock
    fcntl = None                                               # type: ignore[assignment]

# A meta-less slot directory is honoured for this long, dated from its own mtime. mkdir and
# the meta write are not atomic TOGETHER: a competing acquire can catch the winner in that
# few-ms window, see no meta, judge the slot stale, reap it, and win too — an 8-way race
# produced three winners before this grace existed.
META_GRACE_S = 10


class Lease:
    def __init__(self, lease_dir: str | pathlib.Path, max_slots: int = 1):
        self.dir = pathlib.Path(lease_dir).expanduser()
        self.max_slots = max(1, int(max_slots))

    # -- internals ---------------------------------------------------------------------

    def _slot_dir(self, slot: int) -> pathlib.Path:
        return self.dir / f"held.{slot}"

    def _meta_path(self, slot: int) -> pathlib.Path:
        return self._slot_dir(slot) / "meta"

    def _read_meta(self, slot: int) -> dict | None:
        try:
            return json.loads(self._meta_path(slot).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _valid(self, slot: int) -> bool:
        """A lease is valid only if it has not expired AND its owner (if any) is alive."""
        d = self._slot_dir(slot)
        if not d.is_dir():
            return False
        meta = self._read_meta(slot)
        if not meta:
            try:
                age = time.time() - d.stat().st_mtime
            except OSError:
                return False
            return age < META_GRACE_S
        expiry = meta.get("expiry")
        if not isinstance(expiry, (int, float)) or time.time() >= expiry:
            return False
        owner = meta.get("owner")
        if owner in (None, "none"):
            return True
        try:
            os.kill(int(owner), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True                       # exists, owned by someone else — alive
        except (OSError, ValueError):
            return False

    def _existing_slots(self) -> list[int]:
        out = []
        with contextlib.suppress(OSError):
            for p in self.dir.iterdir():
                if p.name.startswith("held.") and p.is_dir():
                    with contextlib.suppress(ValueError):
                        out.append(int(p.name.split(".", 1)[1]))
        return sorted(out)

    def _reap_all(self) -> None:
        """Reap every existing slot, not just 1..max_slots: a slot abandoned while the cap
        was higher must still be reclaimable after it drops, or lowering the limit would
        permanently strand capacity."""
        for slot in self._existing_slots():
            if not self._valid(slot):
                self._remove_slot(slot)

    def _remove_slot(self, slot: int) -> None:
        d = self._slot_dir(slot)
        with contextlib.suppress(OSError):
            self._meta_path(slot).unlink()
        with contextlib.suppress(OSError):
            d.rmdir()

    @contextlib.contextmanager
    def _gate(self):
        """Every mutation runs inside this. Reaping is a check-then-delete: without a gate
        a racer can judge validity BEFORE a winner's mkdir, then remove the winner's brand
        new slot and take it. flock is exactly right for a critical section; only holding
        a lock ACROSS process lifetimes is what the directories are for."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "gate.lock", "a+") as fh:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    # -- API ---------------------------------------------------------------------------

    def try_acquire(self, ttl: float, label: str, owner_pid: int | None = None,
                    max_slots: int | None = None) -> int | None:
        """One non-blocking attempt. Returns the slot number, or None when all slots are
        busy. The caller owns any wait loop — that is what lets a queued run notice a
        cancel request between attempts instead of blocking inside the lock."""
        slots = min(self.max_slots, max_slots) if max_slots else self.max_slots
        with self._gate():
            self._reap_all()
            for slot in range(1, slots + 1):
                d = self._slot_dir(slot)
                try:
                    d.mkdir()
                except FileExistsError:
                    continue
                except OSError:
                    return None
                # meta is written before the gate drops, so no other caller can ever
                # observe this lease mid-creation. If the write itself fails (disk
                # full, permissions) the slot must not be left behind meta-less — it
                # would read as held for the whole grace window.
                try:
                    self._meta_path(slot).write_text(json.dumps({
                        "owner": owner_pid, "label": label,
                        "since": time.time(), "expiry": time.time() + ttl, "slot": slot,
                    }), encoding="utf-8")
                except OSError:
                    self._remove_slot(slot)
                    return None
                return slot
        return None

    def release(self, label: str) -> int:
        """Free the ONE slot held under this label. A labelled release must never touch a
        concurrent run's slot."""
        with self._gate():
            for slot in self._existing_slots():
                meta = self._read_meta(slot) or {}
                if meta.get("label") == label:
                    self._remove_slot(slot)
                    return 1
        return 0

    def release_all(self) -> int:
        """Break-glass: clear every slot."""
        with self._gate():
            slots = self._existing_slots()
            for slot in slots:
                self._remove_slot(slot)
            return len(slots)

    def status(self) -> list[dict]:
        """Currently valid holders (reaped first)."""
        with self._gate():
            self._reap_all()
            out = []
            for slot in self._existing_slots():
                meta = self._read_meta(slot)
                if meta:
                    out.append(meta)
            return out

    def held_count(self) -> int:
        return len(self.status())
