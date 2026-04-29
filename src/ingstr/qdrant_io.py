from typing import Any

from .config import QdrantConfig


class QdrantWriter:
    """Wraps the qdrant-client for the only two operations Ingstr performs:
    upsert_points and delete_points_by_source_path.

    Ingstr does NOT create the collection; it must already exist and have a
    vector size matching `EmbeddingConfig.vector_dim`. A missing collection is a
    fail-fast condition (exit code 2, upstream unavailable).
    """

    def __init__(self, cfg: QdrantConfig, api_key: str) -> None:
        self.cfg = cfg
        self.api_key = api_key

    def assert_collection_exists(self) -> None:
        raise NotImplementedError("QdrantWriter.assert_collection_exists")

    def upsert_points(self, points: list[dict[str, Any]]) -> None:
        raise NotImplementedError("QdrantWriter.upsert_points: batched to cfg.upsert_batch_size")

    def delete_points_by_source_path(self, source_path: str) -> None:
        """Delete all points whose payload.source_path matches the given absolute path."""
        raise NotImplementedError("QdrantWriter.delete_points_by_source_path")

    def health(self) -> bool:
        raise NotImplementedError("QdrantWriter.health")

    def count_points(self) -> int:
        raise NotImplementedError("QdrantWriter.count_points")
