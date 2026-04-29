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
    with patch("ingstr.parse.partition", side_effect=ValueError("malformed pdf")):
        with pytest.raises(IngstrError, match="failed to parse"):
            parse_file(Path("/tmp/bad.pdf"))


def test_wraps_unexpected_exception_types() -> None:
    # unstructured backends throw all sorts (pypdf, lxml, libmagic). The
    # pipeline relies on us catching them all so per-file errors don't abort
    # the whole run.
    with patch("ingstr.parse.partition", side_effect=RuntimeError("libmagic boom")):
        with pytest.raises(IngstrError, match="failed to parse"):
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
