import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pathspec
import structlog

from . import __version__
from .availability import DEFAULT_PURGE_THRESHOLD, PurgeVerdict, assess_purge
from .chunk import Chunk, chunk_elements
from .classify import classify
from .config import OrgView
from .embed import EmbeddingClient
from .exceptions import (
    AmbiguousGrant,
    ConfigError,
    IngstrError,
    RecordedFileError,
    UnclassifiableFile,
    UpstreamUnavailable,
    WrongOrgFile,
)
from .hashing import sha256_file
from .parse import parse_file
from .plan import ResolvedPlan
from .qdrant_io import QdrantPoint, QdrantWriter
from .state import FileRecord, StateDB

#: The verdict a declared `--expect-removals` produces. Separate from the
#: guard's own verdicts so the log line and the reason both say plainly that
#: a person allowed this, rather than that the threshold was satisfied.
_ALLOWED_BY_DECLARATION = PurgeVerdict(
    allowed=True, reason="allowed by a declared --expect-removals"
)

# Stable namespace for deterministic point IDs.
# Changing this would orphan all previously-written points; do not edit.
#
# v0.2.0 changed what is hashed INSIDE this namespace: the id is now
# uuid5(namespace, "<org>:<path below the org's root>:<passage number>"),
# where it was the container-absolute path. The absolute path made the id
# depend on where a reader happened to mount the data, so one deployment
# serving several orgs — which cannot mount them all at the same place —
# would have changed every id. This form does not move when a mount does.
# The change orphaned every point written before v0.2.0; it was made in the
# same release as the embedding-model change, so one full index paid for both
# (one-search-service design, accepted 2026-11-13).
_POINT_NAMESPACE = uuid.UUID("a8f4c0d2-1d5e-4b8a-9d6f-3e2c1a0b7f4e")

_log = structlog.get_logger(__name__)


@dataclass
class RunSummary:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_errored: int = 0
    #: Files refused because their group belongs to another org. Counted apart
    #: from files_errored so "this org's folder holds another org's data" is
    #: visible as itself rather than buried in a parse-failure total.
    files_wrong_org: int = 0
    #: Files that failed before, are unchanged, and were not tried again. They
    #: are counted in files_errored too: the run is not healthier for having
    #: skipped them. This number says how much of the error count was carried
    #: rather than discovered.
    files_failed_skipped: int = 0
    #: Files refused because the folder granting them carries more than one
    #: group. Counted apart because it is the grants that need fixing, not
    #: the files.
    files_ambiguous_grant: int = 0
    #: Passages removed because their file can no longer be classified — its
    #: group left the plan, a grant was withdrawn, or it is now another org's.
    #: Counted apart from files gone from disk: one is access being taken
    #: away, the other is a file being deleted, and an operator reading a
    #: refusal needs to know which. On a dry run or a refusal these hold what
    #: WOULD have been removed, which is where --expect-removals' number
    #: comes from.
    passages_removed_lost_access: int = 0
    passages_removed_gone: int = 0
    #: True when the run reached the step that removes passages. A run that
    #: aborted before it neither raises a refusal nor clears one.
    reached_removal_step: bool = False
    chunks_written: int = 0
    files_deleted: int = 0
    #: Set when ADR-0010 §6 layer 3 refused a purge. The run keeps whatever it
    #: ingested — the refusal is about deletion only — but it must NOT report
    #: success: a withheld purge means the tree we read may not be the tree that
    #: exists, and a silent zero exit would let a broken mount look like a
    #: quiet day.
    purge_refused_reason: str | None = None


@dataclass(frozen=True)
class _ProcessOutcome:
    """Result of processing one file. Categorical so the caller can update
    counters without re-deriving from per-step exceptions."""

    action: str  # "ingest" | "skip_unchanged" | "payload_refresh" | "dry_run"
    #             | "skip_failed"
    chunks: int = 0
    #: Set only for "skip_failed": the error this file recorded when it last
    #: failed. Carried so the run summary can count it as errored WITH its
    #: reason — a skipped failure must not make a run look healthier than it
    #: is, and the exit code stays 3.
    recorded_error: str | None = None


def run_ingest(
    cfg: OrgView,
    plan: ResolvedPlan,
    *,
    state: StateDB,
    embedder: EmbeddingClient,
    qdrant: QdrantWriter,
    full: bool,
    dry_run: bool,
    expect_removals: int | None = None,
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
    #: Files still on disk whose passages must go because the plan no longer
    #: classifies them. Collected here and applied with the orphans below.
    lost_access: set[str] = set()

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
        except OSError as e:
            # The file was listed and is now unreadable or gone — a race with
            # whatever writes this tree, and ordinary on a live share. Before
            # v0.2.1 this escaped to the fatal handler and took the whole run
            # down at exit 4: one file deleted mid-walk discarded every other
            # file's work. A file that cannot be read is that file's result.
            _log.warning("file_unreadable", source_path=abs_path, error=str(e))
            _record_per_file_error(state, path, error=f"could not read: {e}", dry_run=dry_run)
            summary.files_errored += 1
            continue
        except (UnclassifiableFile, AmbiguousGrant) as e:
            _log.warning("file_unclassifiable", source_path=abs_path, error=str(e))
            _record_per_file_error(state, path, error=str(e), dry_run=dry_run)
            summary.files_errored += 1
            if isinstance(e, AmbiguousGrant):
                summary.files_ambiguous_grant += 1
            _note_for_removal(state, abs_path, lost_access)
            continue
        except WrongOrgFile as e:
            _log.error(
                "file_wrong_org",
                source_path=abs_path,
                gid=e.gid,
                group=e.group,
                group_org=e.group_org,
                run_org=e.run_org,
            )
            _record_per_file_error(state, path, error=str(e), dry_run=dry_run)
            summary.files_errored += 1
            summary.files_wrong_org += 1
            _note_for_removal(state, abs_path, lost_access)
            continue
        except UpstreamUnavailable:
            # Systemic failure — Ollama/Qdrant unreachable. Don't swallow:
            # propagate so the CLI can map to exit code 2 and abort the run.
            # Must come BEFORE the IngstrError handler since UpstreamUnavailable
            # is a subclass.
            raise
        except RecordedFileError as e:
            # Already written to the state database, with the group and the
            # version, by the step that knew them. Count it; do not record it
            # twice.
            _log.error("file_failed", source_path=abs_path, error=str(e))
            summary.files_errored += 1
            continue
        except IngstrError as e:
            _log.error("file_failed", source_path=abs_path, error=str(e))
            _record_per_file_error(state, path, error=str(e), dry_run=dry_run)
            summary.files_errored += 1
            continue

        if outcome.action == "skip_failed":
            # Not re-tried, still broken. It counts as errored, with the
            # reason it recorded, so the run reports what is true.
            _log.info(
                "file_failed_before",
                source_path=abs_path,
                error=outcome.recorded_error,
            )
            summary.files_errored += 1
            summary.files_failed_skipped += 1
        elif outcome.action == "skip_unchanged":
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

    # The removal step. TAKING ACCESS AWAY MUST TAKE IT AWAY IN SEARCH, so
    # files that can no longer be classified are removed in EVERY run, not
    # only in --full. Orphans — files gone from disk — are a full-run concern
    # as before, because only a full walk has seen the whole tree.
    known = state.all_paths()
    orphans = sorted(known - seen) if full else []
    lost = sorted(lost_access)

    # A full run ALWAYS reaches the step, including when it finds nothing to
    # remove — that is what lets a clean run clear an earlier refusal. Setting
    # this only when there was something to remove meant: mount breaks, the
    # nightly run refuses; mount comes back, the next nightly run finds
    # nothing, writes no row, and `ingstr stats` keeps showing REMOVAL REFUSED
    # until some later run happens to delete a file. That is the exact case
    # the v0.2.1 fix was for, and this release had quietly undone it.
    if not dry_run and (full or orphans or lost):
        summary.reached_removal_step = True

        # ADR-0010 §6 layer 3. Deletion is the only destructive operation here,
        # and for orphans it is driven by *absence of evidence* — a file not
        # being seen this run. Absence is exactly what a broken mount produces,
        # so the two are indistinguishable from inside this function. The guard
        # does not try to tell them apart; it refuses when the scale of the
        # deletion is implausible, which is the one thing that does not require
        # knowing which happened.
        #
        # Files that lost access count against the same threshold, because a
        # plan read from a half-written file could re-classify everything at
        # once, and that is the same accident wearing different clothes.
        threshold = (
            cfg.agent_source.purge_threshold
            if cfg.agent_source is not None
            else DEFAULT_PURGE_THRESHOLD
        )
        verdict = assess_purge(
            known_count=len(known),
            vanished_count=len(orphans) + len(lost),
            threshold=threshold,
            gone_count=len(orphans),
            lost_access_count=len(lost),
        )
        # An operator who has LOOKED may declare what they expect. A number
        # cannot be typed from habit the way a --force flag can: it has to
        # come from somewhere, and the only place it comes from is a dry run.
        if (
            not verdict.allowed
            and expect_removals is not None
            and len(orphans) + len(lost) <= expect_removals
        ):
            _log.warning(
                "purge_allowed_by_declaration",
                gone=len(orphans),
                lost_access=len(lost),
                declared=expect_removals,
            )
            verdict = _ALLOWED_BY_DECLARATION
        if not verdict.allowed:
            _log.error(
                "purge_refused",
                known=len(known),
                gone=len(orphans),
                lost_access=len(lost),
                threshold=threshold,
                reason=verdict.reason,
            )
            summary.purge_refused_reason = verdict.reason
            summary.passages_removed_gone = len(orphans)
            summary.passages_removed_lost_access = len(lost)
            return summary

        for orphan_path in orphans:
            _log.info("file_orphaned_deleted", source_path=orphan_path)
            qdrant.delete_points_by_source_path(orphan_path)
            state.delete_file(orphan_path)
            summary.files_deleted += 1
            summary.passages_removed_gone += 1

        for lost_path in lost:
            # No source_path in the log: it is org data, and this is otter's
            # journal. The counts say what happened; the paths are in the tree.
            _log.info("file_access_lost_removed")
            qdrant.delete_points_by_source_path(lost_path)
            state.delete_file(lost_path)
            summary.passages_removed_lost_access += 1
    elif dry_run and (orphans or lost):
        # A dry run reports what it WOULD remove and removes nothing. This is
        # where the number for --expect-removals comes from.
        summary.passages_removed_gone = len(orphans)
        summary.passages_removed_lost_access = len(lost)

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


def _file_plan_path(cfg: OrgView, rel_path: str) -> str | None:
    """This file's path as the PLAN would write it, or None if unknowable.

    The plan writes its grant paths relative to a root on beaver; this
    container sees its own mount point. `source.plan_path` is the declared
    part in between — the only thing that relates the two, and knowable only
    to whoever chose what to mount.
    """
    if cfg.source.plan_path is None:
        return None
    return cfg.source.plan_path + rel_path


def _note_for_removal(state: StateDB, abs_path: str, lost_access: set[str]) -> None:
    """Mark a file whose passages must go because it can no longer be
    classified.

    Collected during the walk and applied at the end with the orphans, under
    one guard and one count — not deleted the moment it is found. Two reasons,
    and the second is the one that matters: the guard then sees the TOTAL of
    both kinds before anything is deleted, and a run about to remove too much
    removes nothing at all rather than stopping half way through.

    Only a file with passages is worth noting. One that never indexed has
    nothing to remove, and refusing it is already the whole story.
    """
    existing = state.get_file(abs_path)
    if existing is not None and existing.chunk_count > 0:
        lost_access.add(abs_path)


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _process_file(
    path: Path,
    *,
    cfg: OrgView,
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

    existing = state.get_file(abs_path)
    stat_info = path.stat()

    # 1. classify (raises UnclassifiableFile / WrongOrgFile)
    #
    #    BEFORE the skip below, deliberately. A file refused for its group is
    #    refused by the PLAN, not by anything about the file: a chgrp changes
    #    neither size nor modified time, and a new plan or a new grant changes
    #    nothing in the file at all. Classifying first is what lets the estate
    #    give a folder a group, or add a grant, and have the next ordinary run
    #    pick the files up instead of waiting for 03:00.
    group = classify(
        path,
        plan,
        cfg.org,
        stat_info=stat_info,
        file_plan_path=_file_plan_path(cfg, rel_path),
    )

    # 0. A file that failed before, is unchanged, and failed under THIS version
    #    is not tried again (v0.2.1).
    #
    #    Measured on otter: an unchanged run took 6m24s, of which reading and
    #    hashing every indexed file was 70 seconds. The rest was re-parsing the
    #    520 files that had failed and would fail again — every five minutes,
    #    for as long as they sat there.
    #
    #    Keyed to the VERSION, not only to the file, because the common
    #    failures are facts about the image rather than about the file:
    #    "tesseract is not installed" is one. A record that never expired would
    #    mean the 213 scanned PDFs are never retried after tesseract is added,
    #    by anyone, ever. An upgrade retries them with nobody remembering to.
    #
    #    `--full` tries everything again regardless, which keeps the nightly
    #    run the thorough one — the same division as elsewhere.
    #
    #    A record from before v0.2.1 has no version, so it is retried once and
    #    then recorded properly. That is the right answer, not a special case.
    #    The group is part of the comparison for the same reason: a file that
    #    could not be parsed under one classification should be looked at
    #    again if its classification changes, because that is a different
    #    question about a different audience.
    if (
        not full
        and existing is not None
        and existing.last_error is not None
        and existing.failed_at_version == __version__
        and existing.classification_group == group
        and existing.mtime == stat_info.st_mtime
        and existing.size_bytes == stat_info.st_size
    ):
        return _ProcessOutcome(
            action="skip_failed",
            chunks=0,
            recorded_error=existing.last_error,
        )

    # 2. hash
    content_hash = sha256_file(path)

    now_iso = _utc_now_iso()
    mtime_iso = (
        datetime.fromtimestamp(stat_info.st_mtime, tz=UTC).isoformat()
    )
    file_type = path.suffix.lstrip(".").lower() or "unknown"

    # Hash unchanged → skip work, optionally refresh classification on group change
    if existing is not None and existing.content_hash == content_hash:
        if existing.classification_group == group:
            if existing.last_error is not None and not dry_run:
                # It failed at some point and is now back to content that was
                # indexed successfully. The passages are there and correct, so
                # the recorded failure is stale — clear it rather than count
                # this file as errored for ever.
                #
                # Not in a dry run: "a dry run writes nothing about a file" is
                # only true if it is true of every branch, and this one is
                # easy to miss precisely because clearing a stale error looks
                # harmless.
                state.upsert_file(
                    FileRecord(
                        source_path=existing.source_path,
                        content_hash=existing.content_hash,
                        mtime=stat_info.st_mtime,
                        size_bytes=stat_info.st_size,
                        classification_group=existing.classification_group,
                        chunk_count=existing.chunk_count,
                        last_indexed_at=existing.last_indexed_at,
                        last_error=None,
                        failed_at_version=None,
                    )
                )
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
    #
    #    A failure here is recorded HERE rather than by the caller, because
    #    only this scope knows the file's classification group — and the group
    #    is part of what a later run compares before deciding not to try the
    #    file again. It is also the only failure kind worth not trying again:
    #    a parse cannot start succeeding until the file or the image changes,
    #    while everything else (an unreadable moment, a group that is not in
    #    the plan yet) can.
    try:
        elements = parse_file(
            path, fallback_encodings=cfg.source.fallback_encodings
        )  # IngstrError on failure
        chunks = chunk_elements(elements, cfg.chunking)
        if not chunks:
            raise IngstrError(f"{path}: parsed but produced 0 chunks")
    except UpstreamUnavailable:
        raise  # systemic, not this file's fault — the run aborts
    except IngstrError as e:
        if (
            not dry_run
            and existing is not None
            and existing.chunk_count > 0
            and existing.classification_group != group
        ):
            # The file has passages carrying the OLD group, its content has
            # changed to something that will not parse, and it has been
            # re-classified. Nothing else in the run would ever fix that: the
            # hash no longer matches, so the refresh branch is not reached,
            # and the parse that would re-index it cannot succeed. The
            # passages would sit under the old group for good — readable by
            # the old audience — and if the old content ever came back they
            # would look correct and stay wrong.
            #
            # The same rule as the removal ruling: the index follows access in
            # every run, whether or not the file can be read.
            _log.info(
                "file_group_refreshed_on_failure",
                source_path=abs_path,
                was=existing.classification_group,
                now=group,
            )
            qdrant.set_classification_group(abs_path, group, indexed_at=_utc_now_iso())
        if not dry_run:
            _record_parse_failure(
                state,
                path,
                error=str(e),
                group=group,
                stat_info=stat_info,
                existing=existing,
            )
        raise RecordedFileError(str(e)) from e

    # 4. embed (UpstreamUnavailable propagates)
    texts = [c.text for c in chunks]
    vectors = embedder.embed(texts)

    # 5. build points
    points = [
        _build_point(
            chunk=c,
            vector=v,
            org=cfg.org,
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
    org: str,
    source_path: str,
    source_path_rel: str,
    classification_group: str,
    content_hash: str,
    mtime_iso: str,
    indexed_at: str,
    file_type: str,
) -> QdrantPoint:
    point_id = str(
        uuid.uuid5(_POINT_NAMESPACE, f"{org}:{source_path_rel}:{chunk.index}")
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


def _record_per_file_error(
    state: StateDB, path: Path, *, error: str, dry_run: bool = False
) -> None:
    """Record a per-file error against the file row without overwriting prior
    indexed state. If we have no prior record (file never succeeded), insert a
    minimal placeholder so `ingstr stats` can report it as errored.

    A DRY RUN WRITES NOTHING HERE. `--dry-run` says "plan but do not write",
    and until v0.2.1 that was true of Qdrant and false of the state database —
    a dry run recorded failures, which is both a surprise and, since v0.2.1,
    a way to leave the record disagreeing with the index.
    """
    if dry_run:
        return
    abs_path = str(path.resolve())
    existing = state.get_file(abs_path)

    # The file AS IT IS NOW, not as it was when it last succeeded. v0.2.1
    # decides whether to try a failure again by comparing the stored size and
    # modified time against the file, so they have to describe the version
    # that failed. Before v0.2.1 this branch kept the old values, which was
    # how a file that once indexed and then broke was re-parsed on every run
    # for ever.
    try:
        stat_info = path.stat()
        size = stat_info.st_size
        mtime = stat_info.st_mtime
    except OSError:
        # Gone between failing and being recorded. Zeroes match no real file,
        # so it is tried again if it comes back.
        size = 0
        mtime = 0.0

    if existing is not None:
        # The content hash and the passage count stay as they were: the file
        # still HAS whatever was indexed from it, and a failure is not a
        # reason to forget that.
        state.upsert_file(
            FileRecord(
                source_path=existing.source_path,
                content_hash=existing.content_hash,
                mtime=mtime,
                size_bytes=size,
                classification_group=existing.classification_group,
                chunk_count=existing.chunk_count,
                last_indexed_at=existing.last_indexed_at,
                last_error=error,
                # No version: this kind of failure is not a property of the
                # file and the image, so it is tried again on every run.
                failed_at_version=None,
            )
        )
        return

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
            failed_at_version=None,
        )
    )


def _record_parse_failure(
    state: StateDB,
    path: Path,
    *,
    error: str,
    group: str,
    stat_info: os.stat_result,
    existing: FileRecord | None,
) -> None:
    """Record a parse failure with everything a later run needs to decide.

    The file as it is NOW (size, modified time), the group it classified as,
    and the version that could not read it. A run skips this file only while
    all four still match — so a changed file, a re-classified file, or a new
    image all get another attempt, and nothing else does.
    """
    abs_path = str(path.resolve())
    state.upsert_file(
        FileRecord(
            source_path=abs_path,
            # Whatever was indexed from this file before stays indexed; a
            # failure to re-read it is not a reason to forget it.
            content_hash=existing.content_hash if existing else "",
            mtime=stat_info.st_mtime,
            size_bytes=stat_info.st_size,
            classification_group=group,
            chunk_count=existing.chunk_count if existing else 0,
            last_indexed_at=existing.last_indexed_at if existing else _utc_now_iso(),
            last_error=error,
            failed_at_version=__version__,
        )
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
