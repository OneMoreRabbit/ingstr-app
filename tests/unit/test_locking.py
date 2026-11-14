"""One run per org at a time, and what each kind of run does when it cannot.

The asymmetry is the design, not an oversight: an ordinary run gives way and
SUCCEEDS, because another run is already doing the work and nothing is wrong;
a full run WAITS and fails if it never gets in, because a full run that gives
way silently is exactly what happened on otter — the one nightly run due since the restore
was stopped a second after it started, and nothing said so.
"""

import time
from pathlib import Path

import pytest

from ingstr.locking import RunLockHeld, lock_path_for, run_lock


def test_the_lock_sits_beside_the_state_database(tmp_path: Path) -> None:
    """What it protects is the state, so that is where it lives."""
    db = tmp_path / "state.db"
    assert lock_path_for(db) == tmp_path / "state.db.lock"


def test_one_run_at_a_time(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with run_lock(db), pytest.raises(RunLockHeld), run_lock(db):
        pass


def test_the_lock_is_released_when_the_run_ends(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with run_lock(db):
        pass
    with run_lock(db):
        pass  # free again


def test_the_lock_is_released_even_when_the_run_raises(tmp_path: Path) -> None:
    """A crashed run must not lock the org out. The kernel releases an flock
    when the process ends, which is the property a lock file holding a pid
    does not have — but within one process the context manager must do it."""
    db = tmp_path / "state.db"
    with pytest.raises(ValueError), run_lock(db):
        raise ValueError("the run failed")
    with run_lock(db):
        pass


def test_not_waiting_fails_at_once(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with run_lock(db):
        started = time.monotonic()
        with pytest.raises(RunLockHeld) as exc, run_lock(db, wait_seconds=0):
            pass
        assert time.monotonic() - started < 0.5
        assert "not told it could wait" in str(exc.value)


def test_waiting_gives_up_at_the_limit_and_says_how_long(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with run_lock(db):
        started = time.monotonic()
        with pytest.raises(RunLockHeld) as exc, run_lock(db, wait_seconds=0.3):
            pass
        waited = time.monotonic() - started
        assert 0.25 <= waited < 2.0
        assert "after waiting" in str(exc.value)


def test_a_waiting_run_gets_in_when_the_lock_is_freed(tmp_path: Path) -> None:
    """Otherwise 'waits' would mean 'waits and then fails anyway'."""
    import threading

    db = tmp_path / "state.db"
    holder_in = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with run_lock(db):
            holder_in.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert holder_in.wait(timeout=5)
        release.set()
        with run_lock(db, wait_seconds=5):
            pass  # got in once the other run finished
    finally:
        release.set()
        thread.join(timeout=5)
