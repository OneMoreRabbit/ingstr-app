from typing import Any
from unittest.mock import MagicMock, patch

from ingstr.chunk import Chunk, chunk_elements
from ingstr.config import ChunkingConfig


def _str_element(text: str) -> MagicMock:
    """Build a mock that stringifies to `text`, mimicking unstructured Elements."""
    el = MagicMock()
    el.__str__ = lambda self, t=text: t
    return el


def test_empty_elements_returns_empty_list_without_calling_chunker() -> None:
    with patch("ingstr.chunk.chunk_by_title") as m:
        result = chunk_elements([], ChunkingConfig())
    assert result == []
    m.assert_not_called()


def test_chunker_receives_size_and_overlap_from_config() -> None:
    cfg = ChunkingConfig(chunk_size_chars=1000, chunk_overlap_chars=100)
    fake_chunks = [_str_element("a")]
    elements: list[Any] = [MagicMock()]

    with patch("ingstr.chunk.chunk_by_title", return_value=fake_chunks) as m:
        chunk_elements(elements, cfg)

    m.assert_called_once()
    assert m.call_args.kwargs["max_characters"] == 1000
    assert m.call_args.kwargs["overlap"] == 100


def test_returns_chunks_with_index_and_total() -> None:
    fake_chunks = [_str_element(t) for t in ["one", "two", "three"]]
    with patch("ingstr.chunk.chunk_by_title", return_value=fake_chunks):
        result = chunk_elements([MagicMock()], ChunkingConfig())

    assert len(result) == 3
    assert all(isinstance(c, Chunk) for c in result)
    assert [c.index for c in result] == [0, 1, 2]
    assert all(c.total == 3 for c in result)
    assert [c.text for c in result] == ["one", "two", "three"]


def test_single_chunk_has_total_one() -> None:
    fake_chunks = [_str_element("only one")]
    with patch("ingstr.chunk.chunk_by_title", return_value=fake_chunks):
        result = chunk_elements([MagicMock()], ChunkingConfig())

    assert result == [Chunk(text="only one", index=0, total=1)]
