from pathlib import Path
from typing import Any


def parse_file(path: Path) -> list[Any]:
    """Parse a file into unstructured 'elements'.

    Thin wrapper around `unstructured.partition.auto.partition`. Returns the raw
    element list so the caller can chunk it; we don't materialise text here to
    avoid a redundant pass.
    """
    raise NotImplementedError("parse_file: implement against unstructured.partition.auto.partition")
