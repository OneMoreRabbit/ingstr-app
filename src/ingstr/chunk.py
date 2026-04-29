from dataclasses import dataclass
from typing import Any

from unstructured.chunking.title import chunk_by_title

from .config import ChunkingConfig


@dataclass(frozen=True)
class Chunk:
    """A single chunk of text destined to become one Qdrant point.

    `index` and `total` are stamped onto the Qdrant payload so consumers
    know the chunk's position within its source file.
    """

    text: str
    index: int
    total: int


def chunk_elements(elements: list[Any], cfg: ChunkingConfig) -> list[Chunk]:
    """Split unstructured elements into chunks via the title-respecting strategy.

    Honours `cfg.chunk_size_chars` (max per chunk) and `cfg.chunk_overlap_chars`
    (overlap between adjacent chunks). Returns an empty list for empty input
    rather than calling the underlying chunker.
    """
    if not elements:
        return []

    chunked = chunk_by_title(
        elements,
        max_characters=cfg.chunk_size_chars,
        overlap=cfg.chunk_overlap_chars,
    )

    total = len(chunked)
    return [Chunk(text=str(el), index=i, total=total) for i, el in enumerate(chunked)]
