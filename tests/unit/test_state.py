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
