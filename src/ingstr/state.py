import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    source_path          TEXT PRIMARY KEY,
    content_hash         TEXT NOT NULL,
    mtime                REAL NOT NULL,
    size_bytes           INTEGER NOT NULL,
    classification_group TEXT NOT NULL,
    chunk_count          INTEGER NOT NULL,
    last_indexed_at      TEXT NOT NULL,
    last_error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_group ON files(classification_group);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);

-- ADR-0010 §6 layer 2: continuity. The generation last seen in each export's
-- sentinel. A changed generation means a different incarnation of the export, so
-- an empty or reduced tree is NOT evidence that anything was deleted. Without
-- something persisted across runs, empty-after-rebuild and mass-deletion are the
-- same observation — a within-run check cannot tell them apart at all.
CREATE TABLE IF NOT EXISTS exports (
    export_root   TEXT PRIMARY KEY,
    generation    TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    mode            TEXT NOT NULL,
    files_seen      INTEGER,
    files_indexed   INTEGER,
    files_skipped   INTEGER,
    files_errored   INTEGER,
    chunks_written  INTEGER,
    exit_code       INTEGER
);

-- v0.2.1 adds its state in NEW TABLES rather than new columns, deliberately.
--
-- An older image reads `SELECT * FROM files` and builds its record from the
-- row, so a column this version added is an unexpected field and the older
-- image CRASHES. Measured against v0.1.1: TypeError, FileRecord got an
-- unexpected keyword argument. A new table is simply not looked at, so a roll
-- back keeps working — which is what the estate needs from a roll back.

-- Which image recorded a file's failure, for the failures that are a property
-- of the file and the image together — a parse that cannot succeed until one
-- of them changes. Re-parsing 520 of these every five minutes was five of the
-- six minutes an unchanged run took on otter.
--
-- Keyed to the version because these failures are often facts about the
-- IMAGE: "tesseract is not installed" is one. A record that never expired
-- would mean the 213 scanned PDFs are never retried after tesseract is added,
-- by anyone, ever. An upgrade retries them with nobody remembering to.
--
-- A failure that is a property of the PLAN — unclassifiable, another org's
-- group — is NEVER recorded here: a chgrp or a new plan changes neither the
-- file nor the image, so such a file must be classified again on every run.
CREATE TABLE IF NOT EXISTS file_failures (
    source_path       TEXT PRIMARY KEY,
    failed_at_version TEXT NOT NULL,
    recorded_at       TEXT NOT NULL
);

-- Why a removal was refused, if it was. Kept because a refusal is a STATE,
-- not a moment: until it is cleared, passages of files whose access was taken
-- away can still be found. Before this it existed only in one run's stderr.
CREATE TABLE IF NOT EXISTS run_refusals (
    run_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL
);
"""


def _like_prefix(prefix: str) -> str:
    """Escape a path for use as a LIKE prefix.

    `_` and `%` are wildcards in LIKE, and both are ordinary characters in a
    path — `agent_arc_research_mz` is full of them. Unescaped, that path would
    match other agents' directories and inflate the denominator layer 3 divides
    by, making an implausible purge look plausible.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


@dataclass(frozen=True)
class FileRecord:
    source_path: str
    content_hash: str
    mtime: float
    size_bytes: int
    classification_group: str
    chunk_count: int
    last_indexed_at: str
    last_error: str | None = None
    #: The ingstr version that recorded `last_error`, or None when the record
    #: is not a failure (or predates v0.2.1). Compared against the running
    #: version to decide whether a failure is worth trying again.
    failed_at_version: str | None = None


@dataclass(frozen=True)
class RunRecord:
    run_id: int
    started_at: str
    finished_at: str | None
    mode: str
    files_seen: int | None
    files_indexed: int | None
    files_skipped: int | None
    files_errored: int | None
    chunks_written: int | None
    exit_code: int | None
    #: Set when this run refused to remove passages because too much had
    #: vanished at once. Kept so the refusal can be asked about later: until
    #: it is cleared, passages of files whose access was taken away can still
    #: be found. None on runs that removed normally or removed nothing.
    purge_refused_reason: str | None = None


class StateDB:
    """SQLite-backed state for incremental ingestion and run tracking.

    The DB file is created on first open. All writes are committed per call
    (ingest is sequential and per-file; no transactional batching required).
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        finally:
            cur.close()

    def get_file(self, source_path: str) -> FileRecord | None:
        with self._cursor() as cur:
            row = cur.execute(
                """
                SELECT files.*, file_failures.failed_at_version
                FROM files
                LEFT JOIN file_failures USING (source_path)
                WHERE files.source_path = ?
                """,
                (source_path,),
            ).fetchone()
        return FileRecord(**dict(row)) if row else None

    def upsert_file(self, record: FileRecord) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO files (
                    source_path, content_hash, mtime, size_bytes,
                    classification_group, chunk_count, last_indexed_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    content_hash         = excluded.content_hash,
                    mtime                = excluded.mtime,
                    size_bytes           = excluded.size_bytes,
                    classification_group = excluded.classification_group,
                    chunk_count          = excluded.chunk_count,
                    last_indexed_at      = excluded.last_indexed_at,
                    last_error           = excluded.last_error
                """,
                (
                    record.source_path,
                    record.content_hash,
                    record.mtime,
                    record.size_bytes,
                    record.classification_group,
                    record.chunk_count,
                    record.last_indexed_at,
                    record.last_error,
                ),
            )
            if record.failed_at_version is None:
                cur.execute(
                    "DELETE FROM file_failures WHERE source_path = ?",
                    (record.source_path,),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO file_failures (source_path, failed_at_version, recorded_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(source_path) DO UPDATE SET
                        failed_at_version = excluded.failed_at_version,
                        recorded_at       = excluded.recorded_at
                    """,
                    (record.source_path, record.failed_at_version, record.last_indexed_at),
                )

    def delete_file(self, source_path: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM files WHERE source_path = ?", (source_path,))

    def all_paths(self) -> set[str]:
        with self._cursor() as cur:
            return {row[0] for row in cur.execute("SELECT source_path FROM files")}

    def get_export_generation(self, export_root: str) -> str | None:
        """The generation last seen for this export, or None if never seen.

        None is the honest answer for a first run and must NOT be conflated with
        a mismatch: a first run has nothing to compare against, so it cannot
        conclude a rebuild, and treating unknown as changed would make every
        fresh state DB refuse to purge forever.
        """
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT generation FROM exports WHERE export_root = ?", (export_root,)
            ).fetchone()
        return str(row[0]) if row else None

    def record_export_generation(
        self, export_root: str, generation: str, *, now_iso: str
    ) -> None:
        """Remember this export's generation, preserving when we first saw it.

        `first_seen_at` deliberately survives an update: it is the evidence of
        how long this incarnation has been the one we were reading, which is
        what an operator wants when deciding whether a generation change is a
        rebuild they performed or one they need to explain.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO exports (export_root, generation, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(export_root) DO UPDATE SET
                    generation    = excluded.generation,
                    last_seen_at  = excluded.last_seen_at,
                    first_seen_at = CASE
                        WHEN exports.generation = excluded.generation
                        THEN exports.first_seen_at
                        ELSE excluded.first_seen_at
                    END
                """,
                (export_root, generation, now_iso, now_iso),
            )

    def count_files_under(self, path_prefix: str) -> int:
        """How many known files live under a path prefix.

        The denominator for ADR-0010 §6 layer 3: a purge is judged as a share of
        what we knew about *that scope*, not of the whole collection, so one
        unavailable org cannot be diluted below the threshold by other orgs
        being healthy.
        """
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT COUNT(*) FROM files WHERE source_path LIKE ? ESCAPE '\\'",
                (_like_prefix(path_prefix),),
            ).fetchone()
        return int(row[0])

    def start_run(self, mode: str, started_at: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO runs (started_at, mode) VALUES (?, ?)",
                (started_at, mode),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def count_errored_files(self) -> int:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT COUNT(*) FROM files WHERE last_error IS NOT NULL"
            ).fetchone()
        return int(row[0])

    def get_last_run(
        self,
        *,
        mode: str | None = None,
        success_only: bool = False,
    ) -> RunRecord | None:
        """Most-recent finished run, optionally filtered by mode and exit code."""
        clauses = ["finished_at IS NOT NULL"]
        params: list[object] = []
        if mode is not None:
            clauses.append("mode = ?")
            params.append(mode)
        if success_only:
            clauses.append("exit_code = 0")
        where = " AND ".join(clauses)
        with self._cursor() as cur:
            row = cur.execute(
                f"""
                SELECT runs.*, run_refusals.reason AS purge_refused_reason
                FROM runs
                LEFT JOIN run_refusals ON run_refusals.run_id = CAST(runs.run_id AS TEXT)
                WHERE {where} ORDER BY runs.run_id DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return RunRecord(**dict(row)) if row else None

    def last_refused_removal(self) -> RunRecord | None:
        """The most recent run that refused to remove passages, if any.

        A refusal is a state, not a moment. Until someone clears it, passages
        of files whose access was taken away are still findable — so the
        question "is a removal still outstanding?" has to be answerable after
        the run that refused has gone from the journal.
        """
        with self._cursor() as cur:
            row = cur.execute(
                """
                SELECT runs.*, run_refusals.reason AS purge_refused_reason
                FROM runs
                JOIN run_refusals ON run_refusals.run_id = CAST(runs.run_id AS TEXT)
                ORDER BY runs.run_id DESC LIMIT 1
                """
            ).fetchone()
        return RunRecord(**dict(row)) if row else None

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        files_seen: int,
        files_indexed: int,
        files_skipped: int,
        files_errored: int,
        chunks_written: int,
        exit_code: int,
        purge_refused_reason: str | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE runs SET
                    finished_at    = ?,
                    files_seen     = ?,
                    files_indexed  = ?,
                    files_skipped  = ?,
                    files_errored  = ?,
                    chunks_written = ?,
                    exit_code      = ?
                WHERE run_id = ?
                """,
                (
                    finished_at,
                    files_seen,
                    files_indexed,
                    files_skipped,
                    files_errored,
                    chunks_written,
                    exit_code,
                    run_id,
                ),
            )
            if purge_refused_reason is not None:
                cur.execute(
                    "INSERT INTO run_refusals (run_id, reason) VALUES (?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET reason = excluded.reason",
                    (str(run_id), purge_refused_reason),
                )
