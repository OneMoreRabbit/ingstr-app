"""Orchestration tests for pipeline.run_ingest.

We patch the I/O-bound dependencies (parse, chunk, classify, sha256) at the
pipeline module level and exercise the per-file branching, dry-run gating,
full-mode orphan cleanup, and error categorisation. The state DB is real
(SQLite tmp file); embedder and qdrant are MagicMock.
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from ingstr.chunk import Chunk
from ingstr.config import (
    ChunkingConfig,
    EmbeddingConfig,
    IngstrConfig,
    LoggingConfig,
    OrgConfig,
    OrgView,
    PlanConfig,
    QdrantConfig,
    SourceConfig,
    StateConfig,
)
from ingstr.exceptions import (
    IngstrError,
    UnclassifiableFile,
    UpstreamUnavailable,
    WrongOrgFile,
)
from ingstr.hashing import sha256_file
from ingstr.pipeline import run_ingest
from ingstr.plan import ResolvedPlan
from ingstr.state import FileRecord, StateDB

# ── Fixtures ────────────────────────────────────────────────────────────────


def _cfg(root: Path, db_path: Path, org: str = "test") -> OrgView:
    """One org's resolved view — what the pipeline is handed.

    Built through IngstrConfig.for_org rather than constructed directly, so
    these tests exercise the same resolution path the CLI uses.
    """
    whole = IngstrConfig(
        orgs=[
            OrgConfig(
                org=org,
                source=SourceConfig(root=root, follow_symlinks=False, exclude_patterns=[]),
                qdrant=QdrantConfig(
                    url="http://x",
                    api_key_env="X",
                    collection="c",
                    upsert_batch_size=2,
                    timeout_seconds=5,
                ),
                state=StateConfig(db_path=db_path),
            )
        ],
        plan=PlanConfig(
            compiled_plan_path=root / "_ignored",
            group_gid_map_path=root / "_ignored",
        ),
        embedding=EmbeddingConfig(
            endpoint="http://x", model="m", vector_dim=4, timeout_seconds=5, batch_size=2
        ),
        chunking=ChunkingConfig(),
        logging=LoggingConfig(),
    )
    return whole.for_org(org)


def _plan() -> ResolvedPlan:
    return ResolvedPlan(
        gid_to_group={1003: "arc_g0_engineering_global", 1004: "arc_g18_any_global"},
        required_groups=frozenset({"arc_g0_engineering_global", "arc_g18_any_global"}),
        group_org={
            "arc_g0_engineering_global": "test",
            "arc_g18_any_global": "test",
        },
        grants={},
        plan_source_path=Path("/dev/null"),
        map_source_path=Path("/dev/null"),
    )


@pytest.fixture
def fs(tmp_path: Path):
    """Returns (cfg, plan, state, source_root). Auto-closes state."""
    source_root = tmp_path / "src"
    source_root.mkdir()
    db = tmp_path / "state.db"
    cfg = _cfg(source_root, db)
    state = StateDB(db)
    yield cfg, _plan(), state, source_root
    state.close()


def _write(p: Path, content: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _patches(
    *,
    classify_returns: str | Exception = "arc_g0_engineering_global",
    parse_returns: list[Any] | Exception | None = None,
    chunk_returns: list[Chunk] | None = None,
    hash_returns: str = "deadbeef",
):
    """Build a context-manager-stack of patches for the pipeline module."""
    parse_returns = parse_returns if parse_returns is not None else ["el1"]
    chunk_returns = chunk_returns if chunk_returns is not None else [
        Chunk(text="chunk-0", index=0, total=2),
        Chunk(text="chunk-1", index=1, total=2),
    ]
    return {
        "classify": patch(
            "ingstr.pipeline.classify",
            side_effect=(classify_returns if isinstance(classify_returns, Exception) else None),
            return_value=(None if isinstance(classify_returns, Exception) else classify_returns),
        ),
        "parse_file": patch(
            "ingstr.pipeline.parse_file",
            side_effect=(parse_returns if isinstance(parse_returns, Exception) else None),
            return_value=(None if isinstance(parse_returns, Exception) else parse_returns),
        ),
        "chunk_elements": patch(
            "ingstr.pipeline.chunk_elements", return_value=chunk_returns
        ),
        "sha256_file": patch("ingstr.pipeline.sha256_file", return_value=hash_returns),
    }


@contextmanager
def _applied(patches: dict[str, Any]) -> Iterator[None]:
    """Enter every patch from `_patches()` as a single context.

    Every caller wants the whole stack, which is what `_patches`' docstring
    always promised; entering them by name at each call site just made the
    line too long to read.
    """
    with ExitStack() as stack:
        for p in patches.values():
            stack.enter_context(p)
        yield


def _mock_embedder() -> MagicMock:
    e = MagicMock()
    # Default: 4-dim vector per input, count matches input
    def _embed(texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0, 2.0, 3.0] for _ in texts]
    e.embed.side_effect = _embed
    return e


def _mock_qdrant() -> MagicMock:
    return MagicMock()


# ── Happy path ──────────────────────────────────────────────────────────────


def test_two_new_files_indexed(fs) -> None:
    cfg, plan, state, root = fs
    _write(root / "a.pdf")
    _write(root / "b.pdf")

    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    patches = _patches()
    with _applied(patches):
        summary = run_ingest(
            cfg, plan,
            state=state, embedder=embedder, qdrant=qdrant,
            full=False, dry_run=False,
        )

    assert summary.files_seen == 2
    assert summary.files_indexed == 2
    assert summary.files_skipped == 0
    assert summary.files_errored == 0
    assert summary.chunks_written == 4  # 2 chunks × 2 files
    assert qdrant.upsert_points.call_count == 2
    # State should now know both files.
    assert len(state.all_paths()) == 2


# ── Skip paths ──────────────────────────────────────────────────────────────


def test_unchanged_file_is_skipped(fs) -> None:
    cfg, plan, state, root = fs
    _write(root / "a.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    # First run: index it.
    patches = _patches(hash_returns="hash1")
    with _applied(patches):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    qdrant.reset_mock()
    embedder.reset_mock()

    # Second run: hash unchanged, group unchanged → skip
    patches = _patches(hash_returns="hash1")
    with _applied(patches):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=False, dry_run=False)

    assert summary.files_indexed == 0
    assert summary.files_skipped == 1
    assert summary.chunks_written == 0
    qdrant.upsert_points.assert_not_called()
    qdrant.delete_points_by_source_path.assert_not_called()
    embedder.embed.assert_not_called()


def test_changed_hash_triggers_reindex_with_delete_first(fs) -> None:
    cfg, plan, state, root = fs
    _write(root / "a.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    patches = _patches(hash_returns="h1")
    with _applied(patches):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    qdrant.reset_mock()

    # Hash differs → re-process: delete then upsert
    patches = _patches(hash_returns="h2")
    with _applied(patches):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=False, dry_run=False)

    assert summary.files_indexed == 1
    qdrant.delete_points_by_source_path.assert_called_once()
    qdrant.upsert_points.assert_called_once()


# ── Group-change refresh (full mode contract) ───────────────────────────────


def test_full_mode_refreshes_payload_when_group_changes_without_reembed(fs) -> None:
    cfg, plan, state, root = fs
    _write(root / "a.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    # First run: index with group g0
    patches = _patches(classify_returns="arc_g0_engineering_global", hash_returns="h1")
    with _applied(patches):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    qdrant.reset_mock()
    embedder.reset_mock()

    # Second run, full mode, same hash but new group → set_classification_group
    patches = _patches(classify_returns="arc_g18_any_global", hash_returns="h1")
    with _applied(patches):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=True, dry_run=False)

    assert summary.files_indexed == 1
    qdrant.set_classification_group.assert_called_once()
    call = qdrant.set_classification_group.call_args
    assert call.args[1] == "arc_g18_any_global"
    embedder.embed.assert_not_called()
    qdrant.upsert_points.assert_not_called()


# ── Error paths ─────────────────────────────────────────────────────────────


def test_unclassifiable_file_is_errored_not_aborted(fs) -> None:
    cfg, plan, state, root = fs
    a = _write(root / "a.pdf")
    _write(root / "b.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    # `classify` raises UnclassifiableFile only for "a.pdf"
    def fake_classify(path, _plan, _org, **_kwargs):
        if path.name == "a.pdf":
            raise UnclassifiableFile(path, gid=9999)
        return "arc_g0_engineering_global"

    with patch("ingstr.pipeline.classify", side_effect=fake_classify), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]), \
         patch("ingstr.pipeline.sha256_file", return_value="h"):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=False, dry_run=False)

    assert summary.files_errored == 1
    assert summary.files_indexed == 1
    rec = state.get_file(str(a.resolve()))
    assert rec is not None
    assert rec.last_error is not None
    assert "9999" in rec.last_error


def test_parse_failure_recorded_per_file(fs) -> None:
    cfg, plan, state, root = fs
    a = _write(root / "a.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    with patch("ingstr.pipeline.classify", return_value="arc_g0_engineering_global"), \
         patch("ingstr.pipeline.parse_file", side_effect=IngstrError("bad pdf")), \
         patch("ingstr.pipeline.chunk_elements", return_value=[]), \
         patch("ingstr.pipeline.sha256_file", return_value="h"):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=False, dry_run=False)

    assert summary.files_errored == 1
    rec = state.get_file(str(a.resolve()))
    assert rec is not None and rec.last_error is not None


def test_systemic_error_propagates(fs) -> None:
    cfg, plan, state, root = fs
    _write(root / "a.pdf")
    embedder = _mock_embedder()
    embedder.embed.side_effect = UpstreamUnavailable("ollama down")
    qdrant = _mock_qdrant()

    patches = _patches()
    with _applied(patches), pytest.raises(UpstreamUnavailable, match="ollama"):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)


# ── Dry run ─────────────────────────────────────────────────────────────────


def test_dry_run_does_not_write_to_qdrant_or_state(fs) -> None:
    cfg, plan, state, root = fs
    _write(root / "a.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    patches = _patches()
    with _applied(patches):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=False, dry_run=True)

    assert summary.files_indexed == 1
    assert summary.chunks_written == 2
    qdrant.upsert_points.assert_not_called()
    qdrant.delete_points_by_source_path.assert_not_called()
    assert state.all_paths() == set()


# ── Full mode orphan cleanup ────────────────────────────────────────────────


def test_full_mode_deletes_orphans(fs) -> None:
    cfg, plan, state, root = fs
    a = _write(root / "a.pdf")
    b = _write(root / "b.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    # First run: index both
    patches = _patches()
    with _applied(patches):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    # Remove b.pdf from disk
    b.unlink()
    qdrant.reset_mock()

    patches = _patches()
    with _applied(patches):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=True, dry_run=False)

    assert summary.files_deleted == 1
    qdrant.delete_points_by_source_path.assert_called_once()
    deleted_path = qdrant.delete_points_by_source_path.call_args.args[0]
    assert deleted_path == str(b.resolve())
    # b should be gone from state, a should remain
    assert str(a.resolve()) in state.all_paths()
    assert str(b.resolve()) not in state.all_paths()


# ── Empty parse output ──────────────────────────────────────────────────────


def test_empty_chunks_recorded_as_per_file_error(fs) -> None:
    cfg, plan, state, root = fs
    a = _write(root / "a.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    with patch("ingstr.pipeline.classify", return_value="arc_g0_engineering_global"), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[]), \
         patch("ingstr.pipeline.sha256_file", return_value="h"):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=False, dry_run=False)

    assert summary.files_errored == 1
    rec = state.get_file(str(a.resolve()))
    assert rec is not None and rec.last_error is not None


# ── ADR-0010 §6 layer 3: the purge blast-radius guard ───────────────────────


def test_mass_orphan_purge_is_refused(fs) -> None:
    """The guard, on the path it exists to protect.

    Orphan deletion is driven by *absence of evidence* — a file not seen this
    run — and absence is exactly what a broken mount produces. From inside
    run_ingest the two are indistinguishable, so the guard does not try to tell
    them apart: it refuses when the SCALE is implausible, which needs no
    knowledge of why the files are missing.
    """
    cfg, plan, state, root = fs
    written = [_write(root / f"f{i}.pdf") for i in range(4)]
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    patches = _patches()
    with _applied(patches):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)
    assert len(state.all_paths()) == 4

    # The whole source disappears — a mount failure, or a genuine mass delete.
    for f in written:
        f.unlink()
    qdrant.reset_mock()

    patches = _patches()
    with _applied(patches):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=True, dry_run=False)

    assert summary.purge_refused_reason is not None
    assert "REFUSING to remove passages" in summary.purge_refused_reason
    assert "gone from disk" in summary.purge_refused_reason
    assert "--expect-removals" in summary.purge_refused_reason
    assert summary.files_deleted == 0
    # Nothing deleted anywhere: not from Qdrant, not from state.
    qdrant.delete_points_by_source_path.assert_not_called()
    assert len(state.all_paths()) == 4


def test_orphan_deletion_at_exactly_the_threshold_is_allowed(fs) -> None:
    """Pins the boundary the older orphan test sits on.

    `test_full_mode_deletes_orphans` deletes 1 of 2 known files — exactly 50%,
    the default threshold — and passes because the comparison is strictly
    greater-than. That coupling is easy to miss, so it is asserted here by name
    rather than left implicit in a test about something else.
    """
    cfg, plan, state, root = fs
    a = _write(root / "a.pdf")
    b = _write(root / "b.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    patches = _patches()
    with _applied(patches):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    b.unlink()
    patches = _patches()
    with _applied(patches):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=True, dry_run=False)

    assert summary.purge_refused_reason is None
    assert summary.files_deleted == 1
    assert str(a.resolve()) in state.all_paths()


def test_dry_run_never_reaches_the_guard(fs) -> None:
    """A dry run deletes nothing, so there is nothing to refuse.

    Worth pinning: if the guard ever moved above the `not dry_run` check, a dry
    run would start reporting refusals for deletions it was never going to make.
    """
    cfg, plan, state, root = fs
    written = [_write(root / f"f{i}.pdf") for i in range(4)]
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    patches = _patches()
    with _applied(patches):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)
    for f in written:
        f.unlink()

    patches = _patches()
    with _applied(patches):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                             full=True, dry_run=True)

    assert summary.purge_refused_reason is None
    assert summary.files_deleted == 0


# ── v0.2.0: a file belonging to another org ─────────────────────────────────


def test_wrong_org_file_is_counted_apart_from_other_errors(fs, monkeypatch) -> None:
    """A file in this org's folder owned by another org's group is refused,
    recorded against the file, and counted separately — so "this folder holds
    another org's data" is visible as itself rather than as a parse failure.
    """
    cfg, plan, state, root = fs
    _write(root / "ours.pdf")
    _write(root / "theirs.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    def fake_classify(path, _plan, org, **_kwargs):
        if path.name == "theirs.pdf":
            raise WrongOrgFile(path, gid=1200, group="job_g1", group_org="job", run_org=org)
        return "arc_g0_engineering_global"

    with patch("ingstr.pipeline.classify", side_effect=fake_classify), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]), \
         patch("ingstr.pipeline.sha256_file", return_value="h"):
        summary = run_ingest(
            cfg, plan, state=state, embedder=embedder, qdrant=qdrant, full=False, dry_run=False
        )

    assert summary.files_wrong_org == 1
    assert summary.files_errored == 1
    assert summary.files_indexed == 1
    # The run completes: one bad file does not abort the others.
    assert summary.files_seen == 2


# ── v0.2.1: a file that vanishes, and a failure recorded once ──────────────


def test_a_file_that_vanishes_mid_run_does_not_stop_the_run(fs) -> None:
    """Measured on otter, run 3: a file was listed, then gone when it was
    hashed, and the FileNotFoundError took the whole run down at exit 4 —
    discarding every other file's work for one deleted file. A file that
    cannot be read is that file's result.
    """
    cfg, plan, state, root = fs
    gone = _write(root / "gone.pdf")
    _write(root / "stays.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    real_sha = sha256_file

    def vanish(path: Path) -> str:
        if path.name == "gone.pdf":
            gone.unlink()
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_sha(path)

    with patch("ingstr.pipeline.sha256_file", side_effect=vanish), \
         patch("ingstr.pipeline.classify", return_value="arc_g0_engineering_global"), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        summary = run_ingest(
            cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
            full=False, dry_run=False,
        )

    assert summary.files_errored == 1
    assert summary.files_indexed == 1
    assert summary.files_seen == 2


def test_a_recorded_failure_is_not_tried_again_while_nothing_changes(fs) -> None:
    """The cost this release exists to remove: 520 files failing and being
    re-parsed every five minutes was five of the six minutes an unchanged run
    took."""
    cfg, plan, state, root = fs
    _write(root / "bad.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()
    parse = Mock(side_effect=IngstrError("cannot parse"))

    with patch("ingstr.pipeline.parse_file", parse), \
         patch("ingstr.pipeline.classify", return_value="arc_g0_engineering_global"):
        first = run_ingest(cfg, plan, state=state, embedder=embedder,
                           qdrant=qdrant, full=False, dry_run=False)
        second = run_ingest(cfg, plan, state=state, embedder=embedder,
                            qdrant=qdrant, full=False, dry_run=False)

    assert parse.call_count == 1, "the second run parsed it again"
    # The run is NOT healthier for having skipped it.
    assert second.files_errored == 1
    assert second.files_failed_skipped == 1
    assert first.files_errored == 1


def test_a_full_run_tries_every_failure_again(fs) -> None:
    """The nightly run stays the thorough one — the same division as the
    hashing. Without this, a file that fails under a version is invisible
    until it changes or the version does."""
    cfg, plan, state, root = fs
    _write(root / "bad.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()
    parse = Mock(side_effect=IngstrError("cannot parse"))

    with patch("ingstr.pipeline.parse_file", parse), \
         patch("ingstr.pipeline.classify", return_value="arc_g0_engineering_global"):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=True, dry_run=False)

    assert parse.call_count == 2


def test_a_changed_file_is_tried_again(fs) -> None:
    cfg, plan, state, root = fs
    bad = _write(root / "bad.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()
    parse = Mock(side_effect=IngstrError("cannot parse"))

    with patch("ingstr.pipeline.parse_file", parse), \
         patch("ingstr.pipeline.classify", return_value="arc_g0_engineering_global"):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)
        bad.write_text("different content, different size and time")
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    assert parse.call_count == 2


def test_a_failure_from_another_version_is_tried_again(fs) -> None:
    """The half that stops this becoming a trap. "tesseract is not installed"
    is a fact about the image, not about the file: a record that never expired
    would mean 213 scanned PDFs are never retried after tesseract is added, by
    anyone, ever."""
    cfg, plan, state, root = fs
    bad = _write(root / "bad.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()
    parse = Mock(side_effect=IngstrError("tesseract is not installed"))

    with patch("ingstr.pipeline.parse_file", parse), \
         patch("ingstr.pipeline.classify", return_value="arc_g0_engineering_global"):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    record = state.get_file(str(bad.resolve()))
    assert record is not None and record.failed_at_version is not None
    # The next image is a different version: everything that failed is retried.
    with patch("ingstr.pipeline.parse_file", parse), \
         patch("ingstr.pipeline.classify", return_value="arc_g0_engineering_global"), \
         patch("ingstr.pipeline.__version__", "99.0.0"):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    assert parse.call_count == 2


def test_a_file_refused_for_its_group_is_classified_again_every_run(fs) -> None:
    """The fault arch caught before the tag (hub #13989).

    A file refused for its group is refused by the PLAN, not by anything about
    the file: a chgrp changes neither size nor modified time, and a new plan or
    a new grant changes nothing in the file at all. If such a refusal were
    recorded like a parse failure, giving a folder a group would not be noticed
    by an ordinary run — only by the nightly one at 03:00.
    """
    cfg, plan, state, root = fs
    _write(root / "nogroup.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()
    classify = Mock(side_effect=UnclassifiableFile(root / "nogroup.pdf", gid=9999))

    with patch("ingstr.pipeline.classify", classify):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    assert classify.call_count == 2, "the second run did not classify it again"


def test_a_file_of_another_org_is_classified_again_every_run(fs) -> None:
    """Same reason: whose org a group belongs to is a fact about the plan."""
    cfg, plan, state, root = fs
    _write(root / "theirs.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()
    classify = Mock(side_effect=WrongOrgFile(
        root / "theirs.pdf", gid=1200, group="job_g1", group_org="job", run_org="test"
    ))

    with patch("ingstr.pipeline.classify", classify):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    assert classify.call_count == 2


def test_an_unreadable_moment_is_tried_again(fs) -> None:
    """A share hiccup or a permission being applied is not a property of the
    file. It must not wait for 03:00 either."""
    cfg, plan, state, root = fs
    f = _write(root / "busy.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()
    calls = {"n": 0}

    def flaky(path: Path) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "Permission denied", str(path))
        return "hash-after"

    with patch("ingstr.pipeline.sha256_file", side_effect=flaky), \
         patch("ingstr.pipeline.classify", return_value="arc_g0_engineering_global"), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        first = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                           full=False, dry_run=False)
        second = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                            full=False, dry_run=False)

    assert first.files_errored == 1
    assert second.files_indexed == 1, "the retry never happened"
    assert f.exists()


def test_a_parse_failure_is_tried_again_when_the_group_changes(fs) -> None:
    """A file that could not be parsed under one classification is looked at
    again when its classification changes: a different audience is a different
    question, and it matters once a grant can change a group with no change to
    the file."""
    cfg, plan, state, root = fs
    _write(root / "bad.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()
    parse = Mock(side_effect=IngstrError("cannot parse"))

    with patch("ingstr.pipeline.parse_file", parse), \
         patch("ingstr.pipeline.classify", return_value="arc_g0_engineering_global"):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)
    with patch("ingstr.pipeline.parse_file", parse), \
         patch("ingstr.pipeline.classify", return_value="arc_g18_any_global"):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    assert parse.call_count == 2


def test_a_failed_parse_still_moves_its_passages_to_the_new_group(fs) -> None:
    """arch's second catch before the tag (hub #13992).

    A file indexed under one group, whose content then changes to something
    that will not parse AND whose group has changed: nothing else in the run
    would ever fix the passages. The hash no longer matches, so the refresh
    branch is not reached, and the parse that would re-index it cannot
    succeed. The passages sit under the old group — readable by the old
    audience — and if the old content ever comes back they look correct and
    stay wrong.

    The index follows access in every run, whether or not the file can be read.
    """
    cfg, plan, state, root = fs
    f = _write(root / "was_indexed.pdf", "original")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()

    with patch("ingstr.pipeline.classify", return_value="group_a"), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    f.write_text("content that will not parse")
    qdrant.set_classification_group.reset_mock()

    with patch("ingstr.pipeline.classify", return_value="group_b"), \
         patch("ingstr.pipeline.parse_file", side_effect=IngstrError("cannot parse")):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder,
                             qdrant=qdrant, full=False, dry_run=False)

    assert summary.files_errored == 1
    qdrant.set_classification_group.assert_called_once()
    assert qdrant.set_classification_group.call_args[0][1] == "group_b"
    record = state.get_file(str(f.resolve()))
    assert record is not None and record.classification_group == "group_b"


def test_a_dry_run_moves_nothing(fs) -> None:
    """A dry run plans and does not write — including this."""
    cfg, plan, state, root = fs
    f = _write(root / "was_indexed.pdf", "original")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()

    with patch("ingstr.pipeline.classify", return_value="group_a"), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    f.write_text("content that will not parse")
    qdrant.set_classification_group.reset_mock()

    with patch("ingstr.pipeline.classify", return_value="group_b"), \
         patch("ingstr.pipeline.parse_file", side_effect=IngstrError("cannot parse")):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=True)

    qdrant.set_classification_group.assert_not_called()


def test_a_file_with_no_passages_moves_nothing(fs) -> None:
    """Nothing to move: a file that never indexed has no passages carrying a
    group, so a failed parse writes the record and touches Qdrant not at all."""
    cfg, plan, state, root = fs
    _write(root / "never_indexed.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()

    with patch("ingstr.pipeline.classify", return_value="group_a"), \
         patch("ingstr.pipeline.parse_file", side_effect=IngstrError("cannot parse")):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    qdrant.set_classification_group.assert_not_called()


def test_a_dry_run_writes_no_file_record_at_all(fs) -> None:
    """`--dry-run` says "plan but do not write", and until v0.2.1 that was
    true of Qdrant and false of the state database.

    arch's test, from the code rather than from running it (hub #13994):
    index under one group, change the content and the group, dry run, then a
    real run. If the dry run had written the record, the real run would skip
    the file as "failed before, group matches" and the passages would keep
    the old group for good.
    """
    cfg, plan, state, root = fs
    f = _write(root / "doc.pdf", "original")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()

    with patch("ingstr.pipeline.classify", return_value="group_a"), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    f.write_text("content that will not parse")
    before = state.get_file(str(f.resolve()))
    assert before is not None and before.classification_group == "group_a"

    with patch("ingstr.pipeline.classify", return_value="group_b"), \
         patch("ingstr.pipeline.parse_file", side_effect=IngstrError("cannot parse")):
        dry = run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                         full=False, dry_run=True)

    assert dry.files_errored == 1, "the dry run still reports what it found"
    unchanged = state.get_file(str(f.resolve()))
    assert unchanged == before, "the dry run wrote to the state database"

    qdrant.set_classification_group.reset_mock()
    with patch("ingstr.pipeline.classify", return_value="group_b"), \
         patch("ingstr.pipeline.parse_file", side_effect=IngstrError("cannot parse")):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    qdrant.set_classification_group.assert_called_once()
    assert qdrant.set_classification_group.call_args[0][1] == "group_b"
    after = state.get_file(str(f.resolve()))
    assert after is not None and after.classification_group == "group_b"


def test_a_dry_run_records_no_unclassifiable_file_either(fs) -> None:
    """The same rule for every per-file result, not only the new one."""
    cfg, plan, state, root = fs
    f = _write(root / "nogroup.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()

    with patch(
        "ingstr.pipeline.classify",
        side_effect=UnclassifiableFile(f, gid=9999),
    ):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder,
                             qdrant=qdrant, full=False, dry_run=True)

    assert summary.files_errored == 1
    assert state.get_file(str(f.resolve())) is None


def test_a_dry_run_does_not_even_clear_a_stale_error(fs) -> None:
    """The last branch that wrote during a dry run (arch, hub #13996).

    Clearing a stale failure looks harmless, which is why it was the one
    left — but "a dry run writes nothing about a file" is only true if it is
    true of every branch.
    """
    cfg, plan, state, root = fs
    f = _write(root / "doc.pdf", "original")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()

    with patch("ingstr.pipeline.classify", return_value="group_a"), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    # Leave a stale failure on an otherwise unchanged, correctly indexed file.
    indexed = state.get_file(str(f.resolve()))
    assert indexed is not None
    state.upsert_file(
        FileRecord(
            source_path=indexed.source_path,
            content_hash=indexed.content_hash,
            mtime=indexed.mtime,
            size_bytes=indexed.size_bytes,
            classification_group=indexed.classification_group,
            chunk_count=indexed.chunk_count,
            last_indexed_at=indexed.last_indexed_at,
            last_error="an old failure",
            failed_at_version="0.0.1",
        )
    )
    before = state.get_file(str(f.resolve()))

    with patch("ingstr.pipeline.classify", return_value="group_a"):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=True)

    assert state.get_file(str(f.resolve())) == before

    with patch("ingstr.pipeline.classify", return_value="group_a"):
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    after = state.get_file(str(f.resolve()))
    assert after is not None and after.last_error is None


# ── Taking access away takes it away in search ─────────────────────────────


def _indexed(fs, name: str = "shared.pdf", group: str = "group_a"):
    """Index one file under `group`, and return its path."""
    cfg, plan, state, root = fs
    f = _write(root / name, "content")
    with patch("ingstr.pipeline.classify", return_value=group), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        run_ingest(cfg, plan, state=state, embedder=_mock_embedder(),
                   qdrant=_mock_qdrant(), full=False, dry_run=False)
    return f


def _lose_access_for(name: str):
    """A classify stub that refuses one file and classifies the rest."""
    def _classify(path, _plan, _org, **_kwargs):
        if path.name == name:
            raise UnclassifiableFile(path, gid=65534)
        return "group_a"
    return _classify


def test_a_file_that_can_no_longer_be_classified_loses_its_passages(fs) -> None:
    """The ruling: taking access away must take it away in search.

    Before this, such a file became a per-file error and its passages stayed
    in Qdrant under the old group, indefinitely. Nothing removed them: the
    file is still on disk so it is not an orphan, and it never reaches the
    indexing path so nothing overwrites it. Withdrawing a grant would have
    left every passage of every file in it findable by the revoked group.
    """
    cfg, plan, state, root = fs
    target = _indexed(fs, "shared.pdf")
    for i in range(3):
        _indexed(fs, f"other{i}.pdf")
    qdrant = _mock_qdrant()

    with patch("ingstr.pipeline.classify", side_effect=_lose_access_for("shared.pdf")), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        summary = run_ingest(cfg, plan, state=state, embedder=_mock_embedder(),
                             qdrant=qdrant, full=False, dry_run=False)

    assert summary.passages_removed_lost_access == 1
    qdrant.delete_points_by_source_path.assert_called_once_with(str(target.resolve()))
    assert state.get_file(str(target.resolve())) is None


def test_removal_happens_in_an_ordinary_run_not_only_in_full(fs) -> None:
    """A grant is withdrawn in the plan, not on disk. Waiting for 03:00 to
    act on it is waiting with the access still granted."""
    cfg, plan, state, root = fs
    _indexed(fs, "shared.pdf")
    for i in range(3):
        _indexed(fs, f"other{i}.pdf")
    qdrant = _mock_qdrant()

    with patch("ingstr.pipeline.classify", side_effect=_lose_access_for("shared.pdf")), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        summary = run_ingest(cfg, plan, state=state, embedder=_mock_embedder(),
                             qdrant=qdrant, full=False, dry_run=False)

    assert summary.passages_removed_lost_access == 1


def test_a_file_with_no_passages_is_not_counted_as_a_removal(fs) -> None:
    cfg, plan, state, root = fs
    f = _write(root / "never.pdf")
    qdrant = _mock_qdrant()

    with patch(
        "ingstr.pipeline.classify", side_effect=UnclassifiableFile(f, gid=65534)
    ):
        summary = run_ingest(cfg, plan, state=state, embedder=_mock_embedder(),
                             qdrant=qdrant, full=False, dry_run=False)

    assert summary.passages_removed_lost_access == 0
    qdrant.delete_points_by_source_path.assert_not_called()


def test_a_mass_loss_of_access_is_refused_by_the_same_guard(fs) -> None:
    """A plan read from a half-written file could re-classify everything at
    once — the same accident as a broken mount, wearing different clothes. So
    lost access counts against the same threshold, and a refusal removes
    NOTHING rather than stopping half way."""
    cfg, plan, state, root = fs
    files = [_indexed(fs, f"f{i}.pdf") for i in range(4)]
    qdrant = _mock_qdrant()

    with patch(
        "ingstr.pipeline.classify",
        side_effect=UnclassifiableFile(files[0], gid=65534),
    ):
        summary = run_ingest(cfg, plan, state=state, embedder=_mock_embedder(),
                             qdrant=qdrant, full=False, dry_run=False)

    assert summary.purge_refused_reason is not None
    qdrant.delete_points_by_source_path.assert_not_called()
    # The counts are reported so an operator can act on them.
    assert summary.passages_removed_lost_access == 4
    assert summary.passages_removed_gone == 0


def test_a_declared_number_lets_an_intended_mass_removal_through(fs) -> None:
    """One declared way, and it cannot be typed from habit: the number has to
    come from a dry run."""
    cfg, plan, state, root = fs
    files = [_indexed(fs, f"f{i}.pdf") for i in range(4)]
    qdrant = _mock_qdrant()

    with patch(
        "ingstr.pipeline.classify",
        side_effect=UnclassifiableFile(files[0], gid=65534),
    ):
        summary = run_ingest(cfg, plan, state=state, embedder=_mock_embedder(),
                             qdrant=qdrant, full=False, dry_run=False,
                             expect_removals=4)

    assert summary.purge_refused_reason is None
    assert summary.passages_removed_lost_access == 4
    assert qdrant.delete_points_by_source_path.call_count == 4


def test_a_declared_number_that_is_too_small_still_refuses(fs) -> None:
    """The declaration says what the operator expected. If the world turned
    out otherwise, that is exactly when not to delete."""
    cfg, plan, state, root = fs
    [_indexed(fs, f"f{i}.pdf") for i in range(4)]
    qdrant = _mock_qdrant()

    with patch(
        "ingstr.pipeline.classify",
        side_effect=UnclassifiableFile(Path("/x"), gid=65534),
    ):
        summary = run_ingest(cfg, plan, state=state, embedder=_mock_embedder(),
                             qdrant=qdrant, full=False, dry_run=False,
                             expect_removals=2)

    assert summary.purge_refused_reason is not None
    qdrant.delete_points_by_source_path.assert_not_called()


def test_a_dry_run_reports_what_it_would_remove_and_removes_nothing(fs) -> None:
    """Where the number for --expect-removals comes from."""
    cfg, plan, state, root = fs
    target = _indexed(fs, "shared.pdf")
    for i in range(3):
        _indexed(fs, f"other{i}.pdf")
    qdrant = _mock_qdrant()

    with patch("ingstr.pipeline.classify", side_effect=_lose_access_for("shared.pdf")), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        summary = run_ingest(cfg, plan, state=state, embedder=_mock_embedder(),
                             qdrant=qdrant, full=False, dry_run=True)

    assert summary.passages_removed_lost_access == 1
    qdrant.delete_points_by_source_path.assert_not_called()
    assert state.get_file(str(target.resolve())) is not None


def test_a_clean_full_run_clears_an_earlier_refusal(fs) -> None:
    """arch's fault at c3b5a30 (hub #14020), and it is a regression of the
    v0.2.1 fix.

    The case: the mount breaks, the nightly run refuses. The mount comes back,
    the next nightly run finds nothing to remove — and if that run writes no
    record of having reached the removal step, `ingstr stats` keeps printing
    REMOVAL REFUSED until some later run happens to delete a file. A full run
    that is not a dry run ALWAYS reaches the step, including when there is
    nothing to do there.
    """
    cfg, plan, state, root = fs
    _write(root / "a.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()

    with patch("ingstr.pipeline.classify", return_value="group_a"), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder,
                             qdrant=qdrant, full=True, dry_run=False)

    assert summary.reached_removal_step, (
        "a full run with nothing to remove still reached the removal step, "
        "and must say so or a refusal can never be cleared"
    )
    assert summary.purge_refused_reason is None


def test_an_ordinary_run_with_nothing_to_remove_says_nothing(fs) -> None:
    """The other way round: an ordinary run that removed nothing has no
    opinion about whether a removal is outstanding, and must not clear one."""
    cfg, plan, state, root = fs
    _write(root / "a.pdf")
    embedder, qdrant = _mock_embedder(), _mock_qdrant()

    with patch("ingstr.pipeline.classify", return_value="group_a"), \
         patch("ingstr.pipeline.parse_file", return_value=["el"]), \
         patch("ingstr.pipeline.chunk_elements", return_value=[Chunk("c", 0, 1)]):
        summary = run_ingest(cfg, plan, state=state, embedder=embedder,
                             qdrant=qdrant, full=False, dry_run=False)

    assert not summary.reached_removal_step
