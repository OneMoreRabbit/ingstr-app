from pathlib import Path
from typing import Any

from unstructured.partition.auto import partition

from .exceptions import IngstrError


def parse_file(path: Path) -> list[Any]:
    """Parse a file into unstructured elements via auto-dispatch.

    Wraps `unstructured.partition.auto.partition`, which routes to the right
    backend (pypdf, python-docx, openpyxl, lxml, etc.) by extension or magic
    bytes. Returns the raw element list so the caller can chunk without
    materialising text twice.

    Wraps backend exceptions (which span pypdf, python-docx, lxml, libmagic,
    etc.) in IngstrError so the pipeline can record this as a per-file error
    and proceed with the next file rather than aborting the run.
    """
    try:
        return list(partition(filename=str(path)))
    except Exception as e:
        raise IngstrError(f"failed to parse {path}: {e}") from e
