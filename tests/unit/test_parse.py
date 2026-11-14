from pathlib import Path
from unittest.mock import patch

import pytest

from ingstr.exceptions import IngstrError
from ingstr.parse import parse_file


def test_calls_partition_with_filename_string() -> None:
    with patch("ingstr.parse.partition", return_value=["el1", "el2"]) as m:
        result = parse_file(Path("/tmp/file.pdf"))

    m.assert_called_once()
    # partition can be called with filename= kwarg or first positional; we use kwarg
    assert m.call_args.kwargs.get("filename") == "/tmp/file.pdf"
    assert result == ["el1", "el2"]


def test_returns_list_even_when_partition_returns_iterator() -> None:
    def gen():
        yield "a"
        yield "b"

    with patch("ingstr.parse.partition", return_value=gen()):
        result = parse_file(Path("/tmp/file.txt"))

    assert result == ["a", "b"]


def test_partition_failure_wrapped_in_ingstr_error() -> None:
    with (
        patch("ingstr.parse.partition", side_effect=ValueError("malformed pdf")),
        pytest.raises(IngstrError, match="failed to parse"),
    ):
        parse_file(Path("/tmp/bad.pdf"))


def test_wraps_unexpected_exception_types() -> None:
    # unstructured backends throw all sorts (pypdf, lxml, libmagic). The
    # pipeline relies on us catching them all so per-file errors don't abort
    # the whole run.
    with (
        patch("ingstr.parse.partition", side_effect=RuntimeError("libmagic boom")),
        pytest.raises(IngstrError, match="failed to parse"),
    ):
        parse_file(Path("/tmp/file.docx"))


def test_original_exception_preserved_in_chain() -> None:
    original = ValueError("specific cause")
    with patch("ingstr.parse.partition", side_effect=original):
        try:
            parse_file(Path("/tmp/bad.pdf"))
        except IngstrError as e:
            assert e.__cause__ is original
        else:
            pytest.fail("expected IngstrError")


# ── v0.2.3: two CSV fallbacks, both changing what is indexed ───────────────


def test_a_declared_encoding_is_tried_after_utf8(tmp_path):
    """20 of ARC's CSV files failed with "'utf-8' codec can't decode byte
    0xba" — an ordinary character in cp1252."""
    f = tmp_path / "latin.csv"
    f.write_bytes(b"name,desc\nx,caf\xbae\n")

    with pytest.raises(IngstrError, match="codec can't decode"):
        parse_file(f)

    elements = parse_file(f, fallback_encodings=["cp1252"])
    assert elements
    assert "caf" in " ".join(str(e) for e in elements)


def test_nothing_declared_means_utf8_only(tmp_path):
    """What every version before v0.2.3 did. No detection: a guessed encoding
    produces confident mojibake, which is embedded and searchable and wrong,
    and nothing downstream can tell."""
    f = tmp_path / "latin.csv"
    f.write_bytes(b"name,desc\nx,caf\xbae\n")
    with pytest.raises(IngstrError, match="none declared"):
        parse_file(f, fallback_encodings=[])


def test_an_uneven_table_is_read_as_plain_text(tmp_path):
    """14 of ARC's failed with "Expected 2 fields in line 2, saw 8" — almost
    certainly several tables stacked in one file. Skipping the bad lines would
    drop content silently; the words are what a search index wants."""
    f = tmp_path / "ragged.csv"
    f.write_text("a,b\n1,2,3,4,5,6,7,8\n")
    elements = parse_file(f)
    text = " ".join(str(e) for e in elements)
    assert "a,b" in text
    # Nothing dropped: the long row is there too.
    assert "8" in text
