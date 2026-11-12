from pathlib import Path

from ingstr.state import FileRecord, StateDB


def _record(path: str = "/mnt/raid/a.pdf", *, hash_: str = "h1", chunks: int = 5) -> FileRecord:
    return FileRecord(
        source_path=path,
        content_hash=hash_,
        mtime=1700000000.0,
        size_bytes=1234,
        classification_group="arc_g0_engineering_global",
        chunk_count=chunks,
        last_indexed_at="2026-04-29T10:00:00Z",
        last_error=None,
    )


def test_upsert_and_get(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    db.upsert_file(_record())
    got = db.get_file("/mnt/raid/a.pdf")
    assert got is not None
    assert got.content_hash == "h1"
    assert got.chunk_count == 5
    db.close()


def test_upsert_is_idempotent_and_overwrites(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    db.upsert_file(_record(hash_="h1", chunks=5))
    db.upsert_file(_record(hash_="h2", chunks=12))
    got = db.get_file("/mnt/raid/a.pdf")
    assert got is not None
    assert got.content_hash == "h2"
    assert got.chunk_count == 12
    assert len(db.all_paths()) == 1
    db.close()


def test_delete_file(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    db.upsert_file(_record())
    db.delete_file("/mnt/raid/a.pdf")
    assert db.get_file("/mnt/raid/a.pdf") is None
    db.close()


def test_run_lifecycle(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    run_id = db.start_run(mode="incremental", started_at="2026-04-29T10:00:00Z")
    assert run_id >= 1
    db.finish_run(
        run_id,
        finished_at="2026-04-29T10:05:00Z",
        files_seen=10,
        files_indexed=8,
        files_skipped=1,
        files_errored=1,
        chunks_written=42,
        exit_code=0,
    )
    db.close()


def test_db_persists_across_reopen(tmp_path: Path):
    path = tmp_path / "state.db"
    db = StateDB(path)
    db.upsert_file(_record())
    db.close()

    db2 = StateDB(path)
    got = db2.get_file("/mnt/raid/a.pdf")
    assert got is not None
    db2.close()


# ── ADR-0010 §6 layer 2: export generation continuity ───────────────────────


def test_unknown_export_returns_none_not_a_mismatch(tmp_path: Path) -> None:
    """A first run has nothing to compare against and must say so.

    Conflating "never seen" with "changed" would make every fresh state DB
    refuse to purge forever — a safe-sounding default that quietly disables the
    full-mode cleanup the pipeline exists to do.
    """
    db = StateDB(tmp_path / "s.db")
    assert db.get_export_generation("/mnt/agent-hosts/otter") is None
    db.close()


def test_generation_round_trips(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "s.db")
    db.record_export_generation("/mnt/agent-hosts/otter", "gen-1", now_iso="2026-09-14T00:00:00Z")
    assert db.get_export_generation("/mnt/agent-hosts/otter") == "gen-1"
    db.close()


def test_first_seen_survives_an_unchanged_generation(tmp_path: Path) -> None:
    """Re-recording the same generation must not reset its age.

    first_seen_at is the evidence of how long this incarnation has been the one
    we were reading; resetting it every run would erase exactly the fact an
    operator needs when judging whether a later change was their rebuild.
    """
    db = StateDB(tmp_path / "s.db")
    root = "/mnt/agent-hosts/otter"
    db.record_export_generation(root, "gen-1", now_iso="2026-09-01T00:00:00Z")
    db.record_export_generation(root, "gen-1", now_iso="2026-09-14T00:00:00Z")

    with db._cursor() as cur:
        row = cur.execute(
            "SELECT first_seen_at, last_seen_at FROM exports WHERE export_root = ?", (root,)
        ).fetchone()
    assert row["first_seen_at"] == "2026-09-01T00:00:00Z"
    assert row["last_seen_at"] == "2026-09-14T00:00:00Z"
    db.close()


def test_changed_generation_resets_first_seen(tmp_path: Path) -> None:
    """A new incarnation is a new thing; its age starts now."""
    db = StateDB(tmp_path / "s.db")
    root = "/mnt/agent-hosts/otter"
    db.record_export_generation(root, "gen-1", now_iso="2026-09-01T00:00:00Z")
    db.record_export_generation(root, "gen-2", now_iso="2026-09-14T00:00:00Z")

    with db._cursor() as cur:
        row = cur.execute(
            "SELECT generation, first_seen_at FROM exports WHERE export_root = ?", (root,)
        ).fetchone()
    assert row["generation"] == "gen-2"
    assert row["first_seen_at"] == "2026-09-14T00:00:00Z"
    db.close()


def test_exports_table_is_added_to_an_existing_database(tmp_path: Path) -> None:
    """The schema is CREATE TABLE IF NOT EXISTS, so an old state DB gains it.

    Pins that opening a pre-ADR-0010 database does not need a migration step and
    does not lose its files — the upgrade path operators will actually take.
    """
    path = tmp_path / "s.db"
    first = StateDB(path)
    first.upsert_file(_record("/mnt/raid/a.pdf"))
    first.close()

    second = StateDB(path)
    assert second.get_file("/mnt/raid/a.pdf") is not None
    assert second.get_export_generation("/mnt/agent-hosts/otter") is None
    second.record_export_generation("/mnt/agent-hosts/otter", "g", now_iso="2026-09-14T00:00:00Z")
    assert second.get_export_generation("/mnt/agent-hosts/otter") == "g"
    second.close()


# ── layer 3 denominator ─────────────────────────────────────────────────────


def test_count_files_under_scopes_to_the_prefix(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "s.db")
    for p in ("/mnt/a/one.pdf", "/mnt/a/two.pdf", "/mnt/b/three.pdf"):
        db.upsert_file(_record(p))

    assert db.count_files_under("/mnt/a/") == 2
    assert db.count_files_under("/mnt/b/") == 1
    assert db.count_files_under("/mnt/") == 3
    db.close()


def test_underscores_in_a_path_are_not_wildcards(tmp_path: Path) -> None:
    """`_` matches any character in LIKE, and agent names are full of them.

    Unescaped, `agent_arc_research_mz` would also match `agentXarcYresearchZmz`
    and, more realistically, sibling agents sharing the pattern — inflating the
    denominator layer 3 divides by and making an implausible purge look
    plausible. This is the guard that keeps the blast-radius check honest.
    """
    db = StateDB(tmp_path / "s.db")
    db.upsert_file(_record("/srv/agents/arc/agent_one/memory/a.pdf"))
    db.upsert_file(_record("/srv/agents/arc/agentXone/memory/b.pdf"))

    assert db.count_files_under("/srv/agents/arc/agent_one/") == 1
    db.close()
