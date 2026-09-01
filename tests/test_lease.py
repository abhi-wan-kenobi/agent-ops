"""The counting-semaphore file lease: atomic slots, expiry, owner liveness."""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from agent_ops.lease import Lease

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="flock/kill semantics are POSIX")


def test_acquire_and_release_round_trip(tmp_path):
    lease = Lease(tmp_path / "lease")
    slot = lease.try_acquire(ttl=60, label="run-1", owner_pid=os.getpid())
    assert slot == 1
    assert lease.held_count() == 1
    holders = lease.status()
    assert holders[0]["label"] == "run-1"
    assert holders[0]["owner"] == os.getpid()
    assert lease.release("run-1") == 1
    assert lease.held_count() == 0


def test_default_single_slot_serialises(tmp_path):
    lease = Lease(tmp_path / "lease")
    assert lease.try_acquire(ttl=60, label="a") == 1
    assert lease.try_acquire(ttl=60, label="b") is None, "one slot means one holder"


def test_max_slots_allows_that_many_and_no_more(tmp_path):
    lease = Lease(tmp_path / "lease", max_slots=2)
    assert lease.try_acquire(ttl=60, label="a") == 1
    assert lease.try_acquire(ttl=60, label="b") == 2
    assert lease.try_acquire(ttl=60, label="c") is None


def test_caller_may_cap_itself_below_the_lease_max_but_not_above(tmp_path):
    lease = Lease(tmp_path / "lease", max_slots=3)
    assert lease.try_acquire(ttl=60, label="a", max_slots=1) == 1
    assert lease.try_acquire(ttl=60, label="b", max_slots=1) is None, (
        "a caller-requested cap below the maximum must hold")
    lease2 = Lease(tmp_path / "lease2", max_slots=1)
    assert lease2.try_acquire(ttl=60, label="a", max_slots=5) == 1
    assert lease2.try_acquire(ttl=60, label="b", max_slots=5) is None, (
        "a caller can never raise itself above the lease's own maximum")


def test_expired_lease_is_reaped_and_reacquired(tmp_path):
    lease = Lease(tmp_path / "lease")
    assert lease.try_acquire(ttl=0.05, label="a") == 1
    time.sleep(0.1)
    assert lease.try_acquire(ttl=60, label="b") == 1, "an expired slot must be reclaimable"
    assert [h["label"] for h in lease.status()] == ["b"]


def test_dead_owner_breaks_the_lease_early(tmp_path):
    """A lock whose state you cannot verify is worse than no lock: the owner pid is
    checked against the live process table, not trusted from the timestamp."""
    lease = Lease(tmp_path / "lease")
    slot = lease.try_acquire(ttl=3600, label="crashed", owner_pid=os.getpid())
    meta_path = tmp_path / "lease" / f"held.{slot}" / "meta"
    meta = json.loads(meta_path.read_text())
    meta["owner"] = 2 ** 22 - 17          # comfortably above any real pid on this host
    meta_path.write_text(json.dumps(meta))
    assert lease.try_acquire(ttl=60, label="next") == 1, "dead owner must not hold a slot"


def test_ownerless_lease_stands_on_its_expiry_alone(tmp_path):
    lease = Lease(tmp_path / "lease")
    assert lease.try_acquire(ttl=3600, label="anon", owner_pid=None) == 1
    assert lease.try_acquire(ttl=60, label="b") is None, (
        "no owner pid means expiry is the only invalidator")


def test_metaless_slot_is_honoured_within_the_grace_window(tmp_path):
    """mkdir and the meta write are not atomic together — a competing acquire catching the
    winner in that window must not reap the brand-new slot."""
    lease = Lease(tmp_path / "lease")
    slot_dir = tmp_path / "lease" / "held.1"
    slot_dir.mkdir(parents=True)
    assert lease.try_acquire(ttl=60, label="racer") is None, (
        "a fresh meta-less slot was reaped inside the grace window")


def test_metaless_slot_is_reaped_after_the_grace_window(tmp_path):
    lease = Lease(tmp_path / "lease")
    slot_dir = tmp_path / "lease" / "held.1"
    slot_dir.mkdir(parents=True)
    old = time.time() - 60
    os.utime(slot_dir, (old, old))
    assert lease.try_acquire(ttl=60, label="cleaner") == 1, (
        "a genuinely abandoned meta-less slot must be reclaimed")


def test_labelled_release_touches_only_its_own_slot(tmp_path):
    lease = Lease(tmp_path / "lease", max_slots=2)
    lease.try_acquire(ttl=60, label="a")
    lease.try_acquire(ttl=60, label="b")
    assert lease.release("a") == 1
    assert [h["label"] for h in lease.status()] == ["b"]
    assert lease.release("a") == 0, "releasing an unheld label is a no-op, not an error"


def test_release_all_is_break_glass(tmp_path):
    lease = Lease(tmp_path / "lease", max_slots=2)
    lease.try_acquire(ttl=60, label="a")
    lease.try_acquire(ttl=60, label="b")
    assert lease.release_all() == 2
    assert lease.held_count() == 0


def test_slot_abandoned_above_a_lowered_cap_is_still_reaped(tmp_path):
    """A slot taken while the cap was 3 must be reclaimable after it drops to 1, or
    lowering the limit permanently strands capacity."""
    wide = Lease(tmp_path / "lease", max_slots=3)
    wide.try_acquire(ttl=0.05, label="a")
    wide.try_acquire(ttl=0.05, label="b")
    wide.try_acquire(ttl=0.05, label="c")
    time.sleep(0.1)
    narrow = Lease(tmp_path / "lease", max_slots=1)
    assert narrow.try_acquire(ttl=60, label="fresh") == 1
    assert (tmp_path / "lease" / "held.3").exists() is False, "high slot was not reaped"


def test_concurrent_acquires_produce_exactly_one_winner(tmp_path):
    import concurrent.futures as cf
    lease_dir = tmp_path / "lease"

    def contend(i):
        return Lease(lease_dir).try_acquire(ttl=60, label=f"c{i}", owner_pid=os.getpid())

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(contend, range(8)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"the 8-way race produced {len(winners)} winners: {results}"


def test_meta_write_failure_does_not_strand_the_slot(tmp_path, monkeypatch):
    """Audit finding: a failed meta write left a meta-less slot dir that read as held for
    the whole grace window. The acquire must clean up after itself and report failure."""
    lease = Lease(tmp_path / "lease")

    import pathlib
    real_write = pathlib.Path.write_text

    def failing_write(self, *a, **k):
        if self.name == "meta":
            raise OSError("disk full")
        return real_write(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "write_text", failing_write)
    assert lease.try_acquire(ttl=60, label="a") is None
    monkeypatch.undo()
    assert lease.try_acquire(ttl=60, label="b") == 1, (
        "the failed acquire left a slot behind that blocks the next caller")
