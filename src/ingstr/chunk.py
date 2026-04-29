from dataclasses import dataclass
from typing import Any

from .config import ChunkingConfig


@dataclass(frozen=True)
class Chunk:
    """A single chunk of text destined to become one Qdrant point."""

    text: str
    index: int
    total: int


def chunk_elements(elements: list[Any], cfg: ChunkingConfig) -> list[Chunk]:
    """Split a list of unstructured elements into chunks per the configured strategy.

    MVP: thin wrapper around `unstructured.chunking.title.chunk_by_title`, then
    materialise text. Tracks `index` and `total` so payloads carry a position.
    """
    raise NotImplementedError("chunk_elements: implement against unstructured.chunking.title.chunk_by_title")
