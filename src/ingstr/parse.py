from pathlib import Path
from typing import Any

from unstructured.partition.auto import partition

from .exceptions import IngstrError


def parse_file(path: Path, *, fallback_encodings: list[str] | None = None) -> list[Any]:
    """Parse a file into unstructured elements via auto-dispatch.

    Wraps `unstructured.partition.auto.partition`, which routes to the right
    backend (pypdf, python-docx, openpyxl, lxml, etc.) by extension or magic
    bytes. Returns the raw element list so the caller can chunk without
    materialising text twice.

    Wraps backend exceptions (which span pypdf, python-docx, lxml, libmagic,
    etc.) in IngstrError so the pipeline can record this as a per-file error
    and proceed with the next file rather than aborting the run.

    TWO FALLBACKS, both added in v0.2.3 for failures measured on ARC. Each
    CHANGES WHAT GETS INDEXED, which is why neither is silent in the release
    notes and why neither guesses:

    1. **A declared encoding, after UTF-8.** 20 of ARC's CSV files failed with
       "'utf-8' codec can't decode byte 0xba" — an ordinary character in
       cp1252. The encodings are declared in configuration and tried in order.
       **Not detected:** a guessed encoding produces confident mojibake, which
       is then embedded and searchable and wrong, and nothing downstream can
       tell. An encoding that was declared is at least someone's decision.

    2. **Plain text, when a table will not parse.** 14 of ARC's CSV files
       failed with "Expected 2 fields in line 2, saw 8" — almost certainly
       several tables stacked in one file. Skipping the bad lines would drop
       content silently; reading the whole file as text keeps every word,
       which for a search index is the right answer anyway. The structure is
       lost; the content is not.
    """
    try:
        return list(partition(filename=str(path)))
    except UnicodeDecodeError as e:
        return _retry_with_encodings(path, fallback_encodings or [], original=e)
    except Exception as e:
        if _looks_like_a_table_that_will_not_parse(e):
            return _retry_as_plain_text(path, original=e)
        raise IngstrError(f"failed to parse {path}: {e}") from e


def _retry_with_encodings(
    path: Path, encodings: list[str], *, original: Exception
) -> list[Any]:
    for encoding in encodings:
        try:
            return list(partition(filename=str(path), encoding=encoding))
        except Exception:  # noqa: BLE001, PERF203 — try the next declared one
            continue
    raise IngstrError(
        f"failed to parse {path}: {original}. Tried the declared fallback "
        f"encodings {encodings or '(none declared)'}; declare one in "
        "source.fallback_encodings if this file's encoding is known."
    ) from original


def _looks_like_a_table_that_will_not_parse(error: Exception) -> bool:
    """A delimited file whose rows do not agree on how many fields they have.

    Matched on the exception's type NAME rather than by importing pandas:
    the parser that raises it is unstructured's business, not ingstr's, and
    importing a backend to catch its exception makes this module care which
    backend is in use.
    """
    return type(error).__name__ == "ParserError"


def _retry_as_plain_text(path: Path, *, original: Exception) -> list[Any]:
    try:
        return list(partition(filename=str(path), content_type="text/plain"))
    except Exception as e:
        raise IngstrError(
            f"failed to parse {path}: {original} (and reading it as plain "
            f"text also failed: {e})"
        ) from e
