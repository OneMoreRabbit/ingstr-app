"""Is this export really here, and is it the same one as last time?

ADR-0010 §6: *absence of data is not deletion of data*. Ingstr reads agent
surfaces from a mount of another host's export. When that mount is missing or
broken it can present as an **empty directory**, which is also the legitimate
state of a brand-new agent. Believing the empty reading means concluding the
agent deleted everything and purging its vectors — the agent then goes silently
unsearchable, with a mount as the cause.

Three layers, each catching what the one before it cannot. The ordering is the
design, not an implementation detail:

1. **Presence** — is the export mounted? A sentinel file at the export root.
   Its discriminating power is in its **absence**: the worst case anyone feared,
   a broken mount yielding a *successful empty listing*, handles itself, because
   an empty listing does not contain the sentinel either. This layer is
   therefore correct without knowing NFS's failure semantics, which matters
   because nobody in this estate has ever observed them (ADR-0010 §6 v0.13).
   Any errno while looking also lands here: unreadable is not empty.

2. **Continuity** — is it the *same* export? A sentinel that merely exists
   proves the mount and says nothing about continuity. Rebuild the host,
   recreate the export, write a fresh sentinel, and you get: mounted, sentinel
   present, tree legitimately empty — byte-for-byte identical to "every agent
   deleted everything". So the sentinel carries a generation minted once at
   export creation; we persist the last one seen and refuse to infer deletion
   when it changes.

3. **Blast radius** — the backstop, and the only layer that needs no knowledge
   of NFS or of the export's history. Layers 1 and 2 are both defeated by one
   case: the sentinel served from a stale attribute cache as *present*, carrying
   its old generation, while the tree beneath reads empty. Nothing above can see
   that. So a purge that would remove more than a threshold share of what we
   know refuses, reports, and exits non-zero.

Layer 3 is what makes the unknowns in 1 and 2 non-fatal, which is why it is here
rather than on a backlog. The cases that bite will be ones nobody enumerated.

**None of this is field-validated.** No seat holds an NFS mount, so the tests
prove the *logic* with fault injection, not the *failure semantics*. ADR-0010's
own closure is a deliberate unmount on otter producing a skip-with-note rather
than a purge — that demonstration, at stage 4, is the first field observation
this estate will ever have had. Do not let a green suite be read as that.
"""

from __future__ import annotations

import contextlib
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeVar

SENTINEL_NAME = ".export-ok"

#: The pinned shape (ADR-0010 §6, format PINNED 2026-09-16): exactly one line,
#: `generation=<uuid4>`. One shape only — comparison is string equality with no
#: parsing. The prefix is still *required*: without it any text in the file
#: would become a "generation" that compares equal to itself next run and
#: certifies a continuity nobody established.
_GENERATION_RE = re.compile(r"^generation=(\S+)$")

#: How long to wait on a single filesystem probe before calling it unavailable.
#:
#: ADR-0010 §6 (v0.14): under `rw,hard` an unreachable export **blocks** rather
#: than returning an errno, so "skip and continue" is unreachable exactly when
#: it matters — the run stops with no note, which is the silent failure this
#: design exists to remove, arriving by another door. The deadline makes the
#: outcome correct under both `hard` and `soft`, so ingstr's correctness stops
#: depending on a deployment flag it cannot see from inside the process.
DEFAULT_PROBE_TIMEOUT_SECONDS = 10.0

#: Refuse a purge that would remove more than this share of the files we know
#: about for a scope. Provisional: ansible-platform owes us the mount options
#: they choose (ADR-0010 locality ask §9.2), and `actimeo` sets how wide the
#: layer-3 window really is. Until then this is a judgement call, deliberately
#: cautious — a refusal costs a run, a wrong purge costs an agent's searchability.
DEFAULT_PURGE_THRESHOLD = 0.5


class Availability(Enum):
    """Why a scope is or is not safe to read.

    `UNAVAILABLE` and `EMPTY` are the two readings this module exists to keep
    apart; everything else here is in service of that one distinction.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NEW_INCARNATION = "new_incarnation"


@dataclass(frozen=True)
class ExportState:
    """What we could determine about one export root, and why.

    `reason` is always populated, including on success, because a skipped scope
    has to be a note ingstr emits rather than silence (ADR-0010 §6, and the
    constitution's §9 on partial runs reading like whole ones).
    """

    availability: Availability
    generation: str | None
    reason: str

    @property
    def safe_to_purge(self) -> bool:
        """Only a confirmed-continuous export may drive deletions.

        Deliberately not `availability is AVAILABLE`: a new incarnation is
        readable and ingestible, it just cannot be used to conclude that
        anything was deleted.
        """
        return self.availability is Availability.AVAILABLE


_T = TypeVar("_T")


def _with_deadline(fn: Callable[[], _T], timeout: float) -> tuple[bool, _T | None]:
    """Run `fn`, giving up after `timeout` seconds. Returns (completed, value).

    A blocked NFS syscall under `rw,hard` cannot be interrupted from user space,
    so the worker thread stays parked in the kernel however we wrap it. What we
    control is whether *this run* reaches a verdict and whether the process can
    still exit. A **daemon** thread gives both: we stop waiting after `timeout`,
    and the interpreter will not join it at shutdown.

    `ThreadPoolExecutor` is deliberately not used here, twice over. Its context
    manager calls `shutdown(wait=True)` on exit, so the deadline would expire
    and then the `with` block would wait for the very hang it just gave up on —
    a decorative guard. And since 3.9 its threads are **non-daemon** and joined
    at interpreter exit, so even avoiding the context manager left the process
    unable to exit for as long as the mount stayed wedged. Both were measured,
    not reasoned about: the timeout test took 30s wall-clock instead of 0.05s.

    Deliberately not signal-based either: SIGALRM is main-thread only, which
    would make this unusable from any caller that is not the main thread.
    """
    result: list[_T] = []

    def _runner() -> None:
        with contextlib.suppress(BaseException):
            # Any failure here is carried as "did not complete"; the caller's
            # deadline arm produces the verdict either way.
            result.append(fn())

    worker = threading.Thread(target=_runner, daemon=True, name="atlas-probe")
    worker.start()
    worker.join(timeout)
    if worker.is_alive() or not result:
        return False, None
    return True, result[0]


def _read_sentinel(sentinel: Path) -> tuple[str | None, ExportState | None]:
    """Read one sentinel, mapping every failure to a verdict rather than raising.

    Returns (text, None) on success or (None, verdict) on any failure. Split out
    so it can run under a deadline: the thread does the blocking I/O and the
    caller keeps the ability to give up on it.
    """
    try:
        return sentinel.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        # The load-bearing case. Covers: not mounted; mounted but broken and
        # reporting an empty listing; a bare directory created at the mount
        # point. All three read as "no sentinel", and all three are unavailable.
        return None, ExportState(
            Availability.UNAVAILABLE,
            None,
            f"no {SENTINEL_NAME} at {sentinel.parent} — treating as unreadable, not empty",
        )
    except NotADirectoryError:
        return None, ExportState(
            Availability.UNAVAILABLE,
            None,
            f"{sentinel.parent} is not a directory — treating as unreadable, not empty",
        )
    except OSError as e:
        # ESTALE, EIO, EACCES, ETIMEDOUT and friends. We deliberately do not
        # enumerate errnos: any failure to read the sentinel is unavailability,
        # and an errno we did not anticipate must not fall through to "empty".
        return None, ExportState(
            Availability.UNAVAILABLE,
            None,
            f"cannot read {sentinel} ({e.__class__.__name__}: {e}) — unreadable, not empty",
        )


def read_export_state(
    export_root: Path,
    *,
    last_generation: str | None,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ExportState:
    """Layers 1 and 2 for one export root.

    Never raises for an absent or unreadable mount — that is a reading, not an
    error. It raises only for a caller mistake (a non-Path, say), because
    silently treating a bug as "unavailable" would hide it behind a legitimate
    state, which is the whole failure this module exists to prevent.
    """
    sentinel = export_root / SENTINEL_NAME

    completed, outcome = _with_deadline(lambda: _read_sentinel(sentinel), timeout)
    if not completed:
        # Under `rw,hard` this is what an unreachable export looks like: not an
        # errno, just silence. Without the deadline the run would stop here with
        # no note at all.
        return ExportState(
            Availability.UNAVAILABLE,
            None,
            f"reading {sentinel} exceeded {timeout:g}s — export unresponsive, "
            f"treating as unreadable, not empty",
        )
    raw, error = outcome if outcome is not None else (None, None)

    if error is not None:
        return error
    assert raw is not None

    generation = _parse_generation(raw)
    if generation is None:
        return ExportState(
            Availability.UNAVAILABLE,
            None,
            f"{sentinel} carries no generation — cannot distinguish this export "
            f"from a rebuilt one, so refusing to treat it as continuous",
        )

    if last_generation is None:
        return ExportState(
            Availability.AVAILABLE,
            generation,
            f"first run against export generation {generation}",
        )

    if generation != last_generation:
        return ExportState(
            Availability.NEW_INCARNATION,
            generation,
            f"export generation changed {last_generation} -> {generation}: a different "
            f"incarnation of this export, so an empty or reduced tree is NOT evidence of "
            f"deletion. Skipping; a deliberate re-ingest is required.",
        )

    return ExportState(
        Availability.AVAILABLE,
        generation,
        f"export generation {generation} unchanged",
    )


def _parse_generation(raw: str) -> str | None:
    """Read the pinned one-line `generation=<uuid4>` shape.

    Returns the value for string-equality comparison, or None if the file is
    not that shape. Never invents one: a fabricated generation is worse than
    none, because it compares equal to itself on the next run and certifies a
    continuity that was never established.

    Deliberately strict now that the format is pinned. This previously accepted
    a bare token or JSON, hedging against a format nobody had fixed; ADR-0010
    fixed it on 2026-09-16, so the hedge is retired rather than left to rot into
    a second accepted shape nobody meant to support.
    """
    for line in raw.splitlines():
        m = _GENERATION_RE.match(line.strip())
        if m:
            return m.group(1)
    return None


@dataclass(frozen=True)
class PurgeVerdict:
    """Layer 3: whether a deletion set is plausible, and what to say if not."""

    allowed: bool
    reason: str


def assess_purge(
    *,
    known_count: int,
    vanished_count: int,
    threshold: float = DEFAULT_PURGE_THRESHOLD,
) -> PurgeVerdict:
    """Refuse implausibly large deletions.

    Purge is the only destructive operation in this pipeline and it is driven by
    *absence of evidence* — a file not being seen this run. That makes it the
    one place where a wrong reading is unrecoverable rather than merely noisy,
    so it gets a guard that does not depend on any of the assumptions above
    holding.

    A run that deletes nothing is always allowed; a run against a scope we knew
    nothing about cannot be deleting anything meaningful either.
    """
    if not 0.0 < threshold <= 1.0:
        raise ValueError(f"threshold must be in (0.0, 1.0], got {threshold}")
    if vanished_count < 0 or known_count < 0:
        raise ValueError("counts must be non-negative")

    if vanished_count == 0:
        return PurgeVerdict(True, "nothing to purge")
    if known_count == 0:
        return PurgeVerdict(True, "no prior state for this scope")

    share = vanished_count / known_count
    if share > threshold:
        return PurgeVerdict(
            False,
            f"REFUSING to purge: {vanished_count} of {known_count} known files "
            f"({share:.0%}) are absent this run, over the {threshold:.0%} threshold. "
            f"This is what a broken mount looks like as well as what a real deletion "
            f"looks like, and the two are indistinguishable from here. Re-run once the "
            f"source is known good, or raise the threshold deliberately if the deletion "
            f"is genuine.",
        )
    return PurgeVerdict(
        True,
        f"{vanished_count} of {known_count} ({share:.0%}) absent, within threshold",
    )
