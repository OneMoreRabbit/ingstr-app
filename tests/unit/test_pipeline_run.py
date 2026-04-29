"""Orchestration tests for pipeline.run_ingest.

We patch the I/O-bound dependencies (parse, chunk, classify, sha256) at the
pipeline module level and exercise the per-file branching, dry-run gating,
full-mode orphan cleanup, and error categorisation. The state DB is real
(SQLite tmp file); embedder and qdrant are MagicMock.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ingstr.chunk import Chunk
from ingstr.config import (
    ChunkingConfig,
    EmbeddingConfig,
    IngstrConfig,
    LoggingConfig,
    PlanConfig,
    QdrantConfig,
    SourceConfig,
    StateConfig,
)
from ingstr.exceptions import IngstrError, UnclassifiableFile, UpstreamUnavailable
from ingstr.pipeline import run_ingest
from ingstr.plan import ResolvedPlan
from ingstr.state import StateDB


# ── Fixtures ────────────────────────────────────────────────────────────────


def _cfg(root: Path, db_path: Path) -> IngstrConfig:
    return IngstrConfig(
        org="test",
        source=SourceConfig(root=root, follow_symlinks=False, exclude_patterns=[]),
        plan=PlanConfig(
            compiled_plan_path=root / "_ignored",
            group_gid_map_path=root / "_ignored",
        ),
        embedding=EmbeddingConfig(
            endpoint="http://x", model="m", vector_dim=4, timeout_seconds=5, batch_size=2
        ),
        qdrant=QdrantConfig(
            url="http://x",
            api_key_env="X",
            collection="c",
            upsert_batch_size=2,
            timeout_seconds=5,
        ),
        chunking=ChunkingConfig(),
        state=StateConfig(db_path=db_path),
        logging=LoggingConfig(),
    )


def _plan() -> ResolvedPlan:
    return ResolvedPlan(
        gid_to_group={1003: "arc_g0_engineering_global", 1004: "arc_g18_any_global"},
        required_groups=frozenset({"arc_g0_engineering_global", "arc_g18_any_global"}),
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
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
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
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    qdrant.reset_mock()
    embedder.reset_mock()

    # Second run: hash unchanged, group unchanged → skip
    patches = _patches(hash_returns="hash1")
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
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
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    qdrant.reset_mock()

    # Hash differs → re-process: delete then upsert
    patches = _patches(hash_returns="h2")
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
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
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    qdrant.reset_mock()
    embedder.reset_mock()

    # Second run, full mode, same hash but new group → set_classification_group
    patches = _patches(classify_returns="arc_g18_any_global", hash_returns="h1")
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
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
    def fake_classify(path, _plan):
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
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
        with pytest.raises(UpstreamUnavailable, match="ollama"):
            run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                       full=False, dry_run=False)


# ── Dry run ─────────────────────────────────────────────────────────────────


def test_dry_run_does_not_write_to_qdrant_or_state(fs) -> None:
    cfg, plan, state, root = fs
    _write(root / "a.pdf")
    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    patches = _patches()
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
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
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
        run_ingest(cfg, plan, state=state, embedder=embedder, qdrant=qdrant,
                   full=False, dry_run=False)

    # Remove b.pdf from disk
    b.unlink()
    qdrant.reset_mock()

    patches = _patches()
    with patches["classify"], patches["parse_file"], patches["chunk_elements"], patches["sha256_file"]:
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
