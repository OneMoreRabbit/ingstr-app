"""ADR-0010 §6 — absence of data is not deletion of data.

These prove the **logic** under fault injection. They do not prove NFS's failure
semantics, which nobody in this estate has ever observed: no seat holds a mount.
ADR-0010's closure is a deliberate unmount on otter at stage 4 producing a
skip-with-note rather than a purge, and that will be the first field data there
has been. A green run here is not that, and must not be cited as it.
"""

from pathlib import Path

import pytest

from ingstr.availability import (
    DEFAULT_PURGE_THRESHOLD,
    SENTINEL_NAME,
    Availability,
    assess_purge,
    read_export_state,
)


def _export(tmp_path: Path, generation: str | None = "gen-1") -> Path:
    root = tmp_path / "mnt" / "agent-hosts" / "otter"
    root.mkdir(parents=True)
    if generation is not None:
        (root / SENTINEL_NAME).write_text(generation)
    return root


# ── Layer 1: presence ───────────────────────────────────────────────────────


def test_absent_mount_point_is_unavailable_not_empty(tmp_path: Path) -> None:
    state = read_export_state(tmp_path / "never-mounted", last_generation=None)
    assert state.availability is Availability.UNAVAILABLE
    assert state.safe_to_purge is False


def test_broken_mount_presenting_as_empty_directory_is_unavailable(tmp_path: Path) -> None:
    """The case everyone feared, and the reason a sentinel is sufficient.

    A broken mount that yields a *successful empty listing* is indistinguishable
    from a new agent's empty tree by listing alone. It is not indistinguishable
    by sentinel: an empty listing does not contain `.export-ok` either, so the
    verdict is unavailable without knowing anything about how NFS fails.
    """
    empty = tmp_path / "mounted-but-broken"
    empty.mkdir()
    assert list(empty.iterdir()) == []

    state = read_export_state(empty, last_generation="gen-1")
    assert state.availability is Availability.UNAVAILABLE
    assert state.safe_to_purge is False


def test_bare_directory_created_at_mount_point_is_unavailable(tmp_path: Path) -> None:
    """ingstr does not depend on the write-side rule, and this pins that.

    ADR-0010 §6 records that the apply-path rule forbidding a created
    mount-point directory is load-bearing for rbac-compile and NOT for ingstr:
    such a directory contains no sentinel, so we read unavailable either way.
    If this test ever fails, that recorded independence has stopped being true.
    """
    created = tmp_path / "created-by-apply"
    created.mkdir()
    (created / "some-org").mkdir()

    assert read_export_state(created, last_generation=None).availability is (
        Availability.UNAVAILABLE
    )


def test_unreadable_sentinel_is_unavailable_not_empty(tmp_path: Path) -> None:
    """Any errno is unavailability — stands in for ESTALE/EIO/ETIMEDOUT.

    Permission denied is the errno reachable without a real mount; the code path
    it exercises is the blanket `OSError` arm, which is deliberately not an
    enumeration of errnos so an unanticipated one cannot fall through to
    "empty".
    """
    root = _export(tmp_path)
    (root / SENTINEL_NAME).chmod(0o000)
    try:
        state = read_export_state(root, last_generation="gen-1")
    finally:
        (root / SENTINEL_NAME).chmod(0o644)

    assert state.availability is Availability.UNAVAILABLE
    assert "unreadable" in state.reason


def test_sentinel_path_is_not_a_directory(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "a-file"
    not_a_dir.write_text("x")
    assert read_export_state(not_a_dir, last_generation=None).availability is (
        Availability.UNAVAILABLE
    )


# ── Layer 2: continuity ─────────────────────────────────────────────────────


def test_first_run_records_generation_and_is_available(tmp_path: Path) -> None:
    state = read_export_state(_export(tmp_path, "gen-abc"), last_generation=None)
    assert state.availability is Availability.AVAILABLE
    assert state.generation == "gen-abc"


def test_unchanged_generation_is_available(tmp_path: Path) -> None:
    state = read_export_state(_export(tmp_path, "gen-abc"), last_generation="gen-abc")
    assert state.availability is Availability.AVAILABLE
    assert state.safe_to_purge is True


def test_rebuilt_export_is_not_read_as_mass_deletion(tmp_path: Path) -> None:
    """The hole that a merely-existing sentinel leaves.

    Host rebuilt, export recreated, fresh sentinel written, tree legitimately
    empty — identical to "every agent deleted everything" unless the sentinel
    carries continuity. The verdict must be ingestible-but-not-purgeable.
    """
    state = read_export_state(_export(tmp_path, "gen-NEW"), last_generation="gen-OLD")

    assert state.availability is Availability.NEW_INCARNATION
    assert state.safe_to_purge is False
    assert "NOT evidence of deletion" in state.reason


def test_sentinel_without_generation_refuses_continuity(tmp_path: Path) -> None:
    """An empty sentinel is the pre-v0.3 sentinel, and it cannot discriminate."""
    state = read_export_state(_export(tmp_path, ""), last_generation="gen-1")
    assert state.availability is Availability.UNAVAILABLE
    assert "no generation" in state.reason


@pytest.mark.parametrize(
    "body,expected",
    [
        ("gen-1\n", "gen-1"),
        ("  gen-1  \n\ntrailing junk\n", "gen-1"),
        ('{"generation": "gen-1"}', "gen-1"),
        ('{"epoch": 1757000000}', "1757000000"),
        ('{"uuid": "abc-def"}', "abc-def"),
    ],
)
def test_generation_is_read_from_either_shape(tmp_path: Path, body: str, expected: str) -> None:
    """ansible-platform owns this format; read both shapes rather than guess one."""
    state = read_export_state(_export(tmp_path, body), last_generation=None)
    assert state.generation == expected


@pytest.mark.parametrize("body", ['{"not_a_generation": 1}', "{malformed", "{}", "   "])
def test_unparseable_sentinel_never_invents_a_generation(tmp_path: Path, body: str) -> None:
    """Fabricating one would silently restore the hole layer 2 exists to close."""
    state = read_export_state(_export(tmp_path, body), last_generation="gen-1")
    assert state.generation is None
    assert state.availability is Availability.UNAVAILABLE


# ── Layer 3: blast radius ───────────────────────────────────────────────────


def test_purge_refused_above_threshold() -> None:
    verdict = assess_purge(known_count=100, vanished_count=90)
    assert verdict.allowed is False
    assert "REFUSING" in verdict.reason


def test_purge_allowed_below_threshold() -> None:
    assert assess_purge(known_count=100, vanished_count=5).allowed is True


def test_purge_of_nothing_is_always_allowed() -> None:
    assert assess_purge(known_count=100, vanished_count=0).allowed is True


def test_first_run_with_no_prior_state_is_allowed() -> None:
    assert assess_purge(known_count=0, vanished_count=0).allowed is True


def test_threshold_boundary_is_exclusive() -> None:
    """Exactly at the threshold is allowed; over it is not."""
    assert assess_purge(known_count=10, vanished_count=5, threshold=0.5).allowed is True
    assert assess_purge(known_count=10, vanished_count=6, threshold=0.5).allowed is False


def test_stale_cache_presence_is_caught_only_by_layer_three(tmp_path: Path) -> None:
    """The one case layers 1 and 2 both miss, and the reason layer 3 exists.

    Sentinel served from a stale attribute cache: present, carrying its OLD
    generation, while the tree beneath reads empty. Layer 1 sees a sentinel.
    Layer 2 sees an unchanged generation and says continuous. Both conclude
    "available, safe to purge" — correctly, on the evidence they have. Only the
    size of the resulting deletion gives it away.

    This asserts both halves: that the upper layers really do permit it (so the
    test fails if someone believes they cover this), and that layer 3 refuses.
    """
    root = _export(tmp_path, "gen-cached")
    state = read_export_state(root, last_generation="gen-cached")

    # Layers 1 and 2 permit — they are not wrong, they are blind here.
    assert state.availability is Availability.AVAILABLE
    assert state.safe_to_purge is True

    # Layer 3 is the only thing standing between that and an emptied collection.
    verdict = assess_purge(known_count=2000, vanished_count=2000)
    assert verdict.allowed is False
    assert "broken mount looks like" in verdict.reason


def test_invalid_threshold_rejected() -> None:
    with pytest.raises(ValueError):
        assess_purge(known_count=1, vanished_count=1, threshold=0.0)
    with pytest.raises(ValueError):
        assess_purge(known_count=1, vanished_count=1, threshold=1.5)


def test_default_threshold_is_documented_as_provisional() -> None:
    """Pins the default so a change is deliberate rather than incidental.

    It is a judgement call pending ansible-platform reporting the mount options
    (ADR-0010 locality ask §9.2) — `actimeo` is what actually sets how wide the
    layer-3 window is.
    """
    assert DEFAULT_PURGE_THRESHOLD == 0.5


def test_json_sentinel_that_is_not_an_object_is_rejected(tmp_path: Path) -> None:
    """A JSON array is well-formed JSON and still carries no generation."""
    state = read_export_state(_export(tmp_path, '["gen-1"]'), last_generation="gen-1")
    assert state.generation is None
    assert state.availability is Availability.UNAVAILABLE


def test_negative_counts_rejected() -> None:
    """A caller bug must surface, not resolve to a permissive verdict."""
    with pytest.raises(ValueError, match="non-negative"):
        assess_purge(known_count=-1, vanished_count=0)
    with pytest.raises(ValueError, match="non-negative"):
        assess_purge(known_count=10, vanished_count=-1)


@pytest.mark.parametrize(
    "body",
    [
        '["gen-1"]',                      # JSON array: well-formed, no generation
        '{"generation": true}',           # bool is not an id, and bool is an int in Python
        "this is a sentence not an id",   # prose would compare unequal forever
        "gen 1",                          # whitespace inside the token
    ],
)
def test_sentinel_bodies_that_must_not_yield_a_generation(tmp_path: Path, body: str) -> None:
    """Regression set for fabricated generations.

    `["gen-1"]` is the one that caught a real bug: the JSON branch tested only
    for a leading `{`, so an array fell through to the bare-token branch and the
    literal text became the generation. A fabricated value is worse than none —
    it compares equal to itself next run and silently certifies continuity that
    was never established.

    Prose is the mirror failure: accepted as a token it would differ from
    whatever came before and report a rebuild on every single run.
    """
    state = read_export_state(_export(tmp_path, body), last_generation="gen-1")
    assert state.generation is None
    assert state.availability is Availability.UNAVAILABLE


def test_vanished_files_with_no_known_state_is_allowed() -> None:
    """Nothing known means nothing meaningful to protect.

    Reachable when state was reset but a caller still reports absences. Allowing
    it keeps the guard from blocking a legitimate rebuild of state; there is no
    baseline to judge a proportion against, so there is no signal to act on.
    """
    verdict = assess_purge(known_count=0, vanished_count=5)
    assert verdict.allowed is True
    assert "no prior state" in verdict.reason
