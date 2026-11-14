"""One run per org at a time, enforced by ingstr rather than by a deployment.

Two runs over one org share a state database and a collection. Overlapping,
they double-parse and double-embed, and — worse — one can reach the removal
step and delete points the other has just written.

Until v0.2.3 the estate was left to prevent it, and the cost of that showed:
on otter the two units carried `Conflicts=`, which STOPS the other unit rather
than refusing the new one, so the one nightly full run due since the
restore was stopped a second after it started — and it would have been every
night. No orphan has been removed since, and nothing inside ingstr could see
it, because ingstr was never started.

So the guarantee moves to where the state is. The lock is an `flock` on a file
beside the state database, held for the whole run and released by the kernel
when the process ends — there is nothing to clean up after a crash, which is
the property a lock file with a pid in it does not have.

What a run does when it cannot have it is NOT the same for both kinds, and
that asymmetry is the design (arch's rulings, hub #14046):

* An **ordinary** run gives way immediately and **succeeds**. Another run is
  already doing its work; there is nothing wrong, and a five-minute timer that
  reported failure whenever a long run was in progress would be a check that
  fires when nothing is wrong.
* A **full** run **waits**, and fails if it never gets in. A full run that
  gave way silently is last night again: the thorough run simply never
  happens, and nothing says so.
"""

import fcntl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RunLockHeld(Exception):
    """Another run for this org holds the lock."""

    def __init__(
        self, lock_path: Path, waited_seconds: float, limit_seconds: float
    ) -> None:
        # Branch on what the run was TOLD it could wait, not on how long it
        # happened to take: a run allowed no wait still takes a measurable
        # moment to find out, and "after waiting 0s" reads as a limit that
        # was set to zero rather than one that was never given.
        waited = (
            f" after waiting {waited_seconds:.0f}s of {limit_seconds:.0f}s"
            if limit_seconds > 0
            else " and this run was not told it could wait"
        )
        super().__init__(
            f"another ingstr run for this org holds {lock_path}{waited}"
        )
        self.lock_path = lock_path
        self.waited_seconds = waited_seconds
        self.limit_seconds = limit_seconds


def lock_path_for(db_path: Path) -> Path:
    """Beside the state database, because that is what the lock protects."""
    return db_path.with_name(db_path.name + ".lock")


@contextmanager
def run_lock(db_path: Path, *, wait_seconds: float = 0.0) -> Iterator[None]:
    """Hold the org's run lock, or raise `RunLockHeld`.

    `wait_seconds` 0 means do not wait at all — the ordinary run's behaviour,
    and also a full run that has not been told how long it may wait. Waiting
    is polled rather than blocking so the limit is honoured exactly; the poll
    is cheap and a run is minutes long.
    """
    path = lock_path_for(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    started = time.monotonic()
    handle = path.open("w")
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RunLockHeld(
                        path, time.monotonic() - started, wait_seconds
                    ) from None
                time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
