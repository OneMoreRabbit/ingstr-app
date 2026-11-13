"""The passage id, and why it changed in v0.2.0.

Until v0.2.0 the id was uuid5 over the CONTAINER-ABSOLUTE path, so it depended
on where a reader happened to mount the data. One deployment serving several
orgs cannot mount them all at the same place, so every id would have changed
the first time the mount moved — and the first run after that writes a second
complete set of points while the old set is orphaned.

The id is now uuid5 over "<org>:<path below the org's root>:<passage number>".
Two properties are worth a test each: it does not move when the mount does,
and it separates orgs that share a relative path.
"""

import uuid
from pathlib import Path

from ingstr.pipeline import _POINT_NAMESPACE, _build_point


def _id(*, org: str, rel: str, index: int, abs_path: str) -> str:
    point = _build_point(
        chunk=type("C", (), {"text": "t", "index": index, "total": 1})(),
        vector=[0.0],
        org=org,
        source_path=abs_path,
        source_path_rel=rel,
        classification_group="g",
        content_hash="h",
        mtime_iso="2026-01-01T00:00:00+00:00",
        indexed_at="2026-01-01T00:00:00+00:00",
        file_type="pdf",
    )
    return point.id


def test_id_does_not_move_when_the_mount_does() -> None:
    a = _id(org="arc", rel="dir/f.pdf", index=0, abs_path="/mnt/data/dir/f.pdf")
    b = _id(org="arc", rel="dir/f.pdf", index=0, abs_path="/srv/arc/dir/f.pdf")
    assert a == b


def test_two_orgs_sharing_a_relative_path_get_different_ids() -> None:
    """The collision the absolute path used to hide: with every org mounted at
    /mnt/data, the same relative path in two orgs produced the same id."""
    arc = _id(org="arc", rel="shared/name.pdf", index=0, abs_path="/mnt/data/shared/name.pdf")
    job = _id(org="job", rel="shared/name.pdf", index=0, abs_path="/mnt/data/shared/name.pdf")
    assert arc != job


def test_passage_number_separates_chunks_of_one_file() -> None:
    first = _id(org="arc", rel="f.pdf", index=0, abs_path="/mnt/data/f.pdf")
    second = _id(org="arc", rel="f.pdf", index=1, abs_path="/mnt/data/f.pdf")
    assert first != second


def test_id_is_the_documented_derivation() -> None:
    """Stated as a value, not only as a property: the contract names this
    derivation, and dprox's stored ids all changed when it did."""
    expected = str(uuid.uuid5(_POINT_NAMESPACE, "arc:dir/f.pdf:3"))
    assert _id(org="arc", rel="dir/f.pdf", index=3, abs_path="/anywhere/dir/f.pdf") == expected


def test_the_v0_1_derivation_no_longer_matches() -> None:
    """The change orphaned every point written before v0.2.0. That is the fact
    the release notes and the contract's major bump rest on, so it is pinned
    here rather than left as a claim."""
    old = str(uuid.uuid5(_POINT_NAMESPACE, "/mnt/data/dir/f.pdf:0"))
    new = _id(org="arc", rel="dir/f.pdf", index=0, abs_path="/mnt/data/dir/f.pdf")
    assert old != new


def test_relative_path_is_posix_so_ids_do_not_depend_on_the_host(tmp_path: Path) -> None:
    """rel comes from Path.relative_to(...).as_posix() in the pipeline; this
    pins that the id is built from that form, not from a native path string."""
    assert _id(org="arc", rel="a/b.pdf", index=0, abs_path="/x") == str(
        uuid.uuid5(_POINT_NAMESPACE, "arc:a/b.pdf:0")
    )
