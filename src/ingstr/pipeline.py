import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pathspec
import structlog

from .chunk import Chunk, chunk_elements
from .classify import classify
from .config import IngstrConfig
from .embed import EmbeddingClient
from .exceptions import ConfigError, IngstrError, UnclassifiableFile, UpstreamUnavailable
from .hashing import sha256_file
from .parse import parse_file
from .plan import ResolvedPlan
from .qdrant_io import QdrantPoint, QdrantWriter
from .state import FileRecord, StateDB

# Stable namespace for deterministic point IDs.
# Changing this would orphan all previously-written points; do not edit.
_POINT_NAMESPACE = uuid.UUID("a8f4c0d2-1d5e-4b8a-9d6f-3e2c1a0b7f4e")

_log = structlog.get_logger(__name__)


@dataclass
class RunSummary:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_errored: int = 0
    chunks_written: int = 0
    files_deleted: int = 0


@dataclass(frozen=True)
class _ProcessOutcome:
    """Result of processing one file. Categorical so the caller can update
    counters without re-deriving from per-step exceptions."""

    action: str  # "ingest" | "skip_unchanged" | "payload_refresh" | "dry_run"
    chunks: int = 0


def run_ingest(
    cfg: IngstrConfig,
    plan: ResolvedPlan,
    *,
    state: StateDB,
    embedder: EmbeddingClient,
    qdrant: QdrantWriter,
    full: bool,
    dry_run: bool,
) -> RunSummary:
    """Walk source.root and run the per-file pipeline (brief §8).

    Per-file errors (unclassifiable, parse failures, empty files) are recorded
    in the state DB and counted but do not abort the run. Systemic errors
    (Qdrant unreachable, Ollama unreachable, plan unreadable) propagate up so
    the caller can map to exit code 2.

    In `full` mode, also deletes points + state rows for files no longer on
    disk, and refreshes the `classification_group` payload (without
    re-embedding) where the filesystem GID has changed.
    """
    summary = RunSummary()
    seen: set[str] = set()

    for path in iter_source_files(
        cfg.source.root,
        cfg.source.follow_symlinks,
        cfg.source.exclude_patterns,
    ):
        summary.files_seen += 1
        abs_path = str(path.resolve())
        seen.add(abs_path)
        try:
            outcome = _process_file(
                path,
                cfg=cfg,
                plan=plan,
                state=state,
                embedder=embedder,
                qdrant=qdrant,
                full=full,
                dry_run=dry_run,
            )
        except UnclassifiableFile as e:
            _log.warning(
                "file_unclassifiable", source_path=abs_path, gid=e.gid
            )
            _record_per_file_error(state, path, error=str(e))
            summary.files_errored += 1
            continue
        except IngstrError as e:
            _log.error("file_failed", source_path=abs_path, error=str(e))
            _record_per_file_error(state, path, error=str(e))
            summary.files_errored += 1
            continue

        if outcome.action == "skip_unchanged":
            summary.files_skipped += 1
        elif outcome.action == "payload_refresh":
            summary.files_indexed += 1
        elif outcome.action == "ingest":
            summary.files_indexed += 1
            summary.chunks_written += outcome.chunks
        elif outcome.action == "dry_run":
            # Dry run: count as "would-have-indexed" without writing.
            summary.files_indexed += 1
            summary.chunks_written += outcome.chunks

    if full and not dry_run:
        known = state.all_paths()
        orphans = sorted(known - seen)
        for orphan_path in orphans:
            _log.info("file_orphaned_deleted", source_path=orphan_path)
            qdrant.delete_points_by_source_path(orphan_path)
            state.delete_file(orphan_path)
            summary.files_deleted += 1

    return summary


def iter_source_files(
    root: Path,
    follow_symlinks: bool,
    exclude_patterns: list[str],
) -> Iterator[Path]:
    """Yield candidate file paths under `root`, applying gitignore-style excludes
    against paths relative to `root`."""
    if not root.is_dir():
        raise ConfigError(f"source.root is not a directory: {root}")

    spec = (
        pathspec.GitIgnoreSpec.from_lines(exclude_patterns)
        if exclude_patterns
        else None
    )

    for current_root, dirs, files in os.walk(root, followlinks=follow_symlinks):
        current = Path(current_root)
        if spec is not None:
            # Prune excluded directories so we don't descend into them.
            dirs[:] = [
                d for d in dirs
                if not spec.match_file(_rel_posix(current / d, root) + "/")
            ]
        for name in files:
            path = current / name
            if spec is not None and spec.match_file(_rel_posix(path, root)):
                continue
            yield path


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _process_file(
    path: Path,
    *,
    cfg: IngstrConfig,
    plan: ResolvedPlan,
    state: StateDB,
    embedder: EmbeddingClient,
    qdrant: QdrantWriter,
    full: bool,
    dry_run: bool,
) -> _ProcessOutcome:
    """Per-file pipeline, brief §8 steps 1–9."""
    abs_path = str(path.resolve())
    rel_path = path.relative_to(cfg.source.root).as_posix()

    # 1. classify (raises UnclassifiableFile)
    group = classify(path, plan)

    # 2. hash + stat
    stat_info = path.stat()
    content_hash = sha256_file(path)

    existing = state.get_file(abs_path)
    now_iso = _utc_now_iso()
    mtime_iso = (
        datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc).isoformat()
    )
    file_type = path.suffix.lstrip(".").lower() or "unknown"

    # Hash unchanged → skip work, optionally refresh classification on group change
    if existing is not None and existing.content_hash == content_hash:
        if existing.classification_group == group:
            return _ProcessOutcome(action="skip_unchanged", chunks=existing.chunk_count)

        # GID changed but content didn't. Refresh the payload field on every
        # existing point without re-embedding (brief §4 full-mode contract).
        if dry_run:
            return _ProcessOutcome(action="dry_run", chunks=existing.chunk_count)
        qdrant.set_classification_group(abs_path, group, indexed_at=now_iso)
        state.upsert_file(
            FileRecord(
                source_path=abs_path,
                content_hash=content_hash,
                mtime=stat_info.st_mtime,
                size_bytes=stat_info.st_size,
                classification_group=group,
                chunk_count=existing.chunk_count,
                last_indexed_at=now_iso,
                last_error=None,
            )
        )
        _log.info(
            "file_payload_refreshed",
            source_path=abs_path,
            old_group=existing.classification_group,
            new_group=group,
        )
        return _ProcessOutcome(action="payload_refresh", chunks=existing.chunk_count)

    # 3. parse + chunk
    elements = parse_file(path)  # IngstrError on failure
    chunks = chunk_elements(elements, cfg.chunking)
    if not chunks:
        raise IngstrError(f"{path}: parsed but produced 0 chunks")

    # 4. embed (UpstreamUnavailable propagates)
    texts = [c.text for c in chunks]
    vectors = embedder.embed(texts)

    # 5. build points
    points = [
        _build_point(
            chunk=c,
            vector=v,
            source_path=abs_path,
            source_path_rel=rel_path,
            classification_group=group,
            content_hash=content_hash,
            mtime_iso=mtime_iso,
            indexed_at=now_iso,
            file_type=file_type,
        )
        for c, v in zip(chunks, vectors, strict=True)
    ]

    if dry_run:
        return _ProcessOutcome(action="dry_run", chunks=len(chunks))

    # 6. delete old points if this file was previously indexed
    if existing is not None:
        qdrant.delete_points_by_source_path(abs_path)

    # 7. upsert
    qdrant.upsert_points(points)

    # 8. update state
    state.upsert_file(
        FileRecord(
            source_path=abs_path,
            content_hash=content_hash,
            mtime=stat_info.st_mtime,
            size_bytes=stat_info.st_size,
            classification_group=group,
            chunk_count=len(chunks),
            last_indexed_at=now_iso,
            last_error=None,
        )
    )
    _log.info(
        "file_indexed",
        source_path=abs_path,
        classification_group=group,
        chunk_count=len(chunks),
    )
    return _ProcessOutcome(action="ingest", chunks=len(chunks))


def _build_point(
    *,
    chunk: Chunk,
    vector: list[float],
    source_path: str,
    source_path_rel: str,
    classification_group: str,
    content_hash: str,
    mtime_iso: str,
    indexed_at: str,
    file_type: str,
) -> QdrantPoint:
    point_id = str(
        uuid.uuid5(_POINT_NAMESPACE, f"{source_path}:{chunk.index}")
    )
    return QdrantPoint(
        id=point_id,
        vector=vector,
        payload={
            "text": chunk.text,
            "source_path": source_path,
            "source_path_rel": source_path_rel,
            "classification_group": classification_group,
            "modified_at": mtime_iso,
            "indexed_at": indexed_at,
            "file_type": file_type,
            "chunk_index": chunk.index,
            "chunk_total": chunk.total,
            "content_hash": content_hash,
        },
    )


def _record_per_file_error(state: StateDB, path: Path, *, error: str) -> None:
    """Record a per-file error against the file row without overwriting prior
    indexed state. If we have no prior record (file never succeeded), insert a
    minimal placeholder so `ingstr stats` can report it as errored."""
    abs_path = str(path.resolve())
    existing = state.get_file(abs_path)
    if existing is not None:
        state.upsert_file(
            FileRecord(
                source_path=existing.source_path,
                content_hash=existing.content_hash,
                mtime=existing.mtime,
                size_bytes=existing.size_bytes,
                classification_group=existing.classification_group,
                chunk_count=existing.chunk_count,
                last_indexed_at=existing.last_indexed_at,
                last_error=error,
            )
        )
        return

    try:
        stat_info = path.stat()
        size = stat_info.st_size
        mtime = stat_info.st_mtime
    except OSError:
        size = 0
        mtime = 0.0
    state.upsert_file(
        FileRecord(
            source_path=abs_path,
            content_hash="",
            mtime=mtime,
            size_bytes=size,
            classification_group="",
            chunk_count=0,
            last_indexed_at=_utc_now_iso(),
            last_error=error,
        )
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
