from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.http.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from .config import QdrantConfig
from .exceptions import UpstreamUnavailable


@dataclass(frozen=True)
class QdrantPoint:
    """One Qdrant point: deterministic ID + vector + payload (brief §9).

    The pipeline builds these per chunk; deterministic UUIDs (uuid5 of
    `source_path:chunk_index`) make re-runs idempotent — the same chunk
    overwrites the same point rather than creating a duplicate.
    """

    id: str
    vector: list[float]
    payload: dict[str, Any]


class QdrantWriter:
    """Wraps qdrant-client for the operations Ingstr performs against an
    already-provisioned collection: assert/health, upsert, delete-by-source,
    and count.

    Ingstr does NOT create the collection. `assert_collection_exists` fails
    fast on a missing collection or a mismatched vector dim, so a malformed
    deploy is caught before any work happens.
    """

    def __init__(
        self,
        cfg: QdrantConfig,
        api_key: str,
        *,
        client: QdrantClient | None = None,
    ) -> None:
        self.cfg = cfg
        self._client = client or QdrantClient(
            url=cfg.url,
            api_key=api_key,
            timeout=cfg.timeout_seconds,
        )

    def assert_collection_exists(self, expected_vector_dim: int) -> None:
        """Confirm the collection exists with the expected vector dim, else raise.

        Distinguishes 404 (missing collection — operator deploy issue) from
        other failures (network, auth) so the error message points at the
        right thing to fix.
        """
        try:
            info = self._client.get_collection(self.cfg.collection)
        except UnexpectedResponse as e:
            if e.status_code == 404:
                raise UpstreamUnavailable(
                    f"qdrant collection '{self.cfg.collection}' does not exist on "
                    f"{self.cfg.url}; provision it (Ansible) before running ingstr"
                ) from e
            raise UpstreamUnavailable(
                f"qdrant get_collection({self.cfg.collection}) failed: {e}"
            ) from e
        except Exception as e:
            raise UpstreamUnavailable(
                f"qdrant unreachable at {self.cfg.url}: {e}"
            ) from e

        # MVP: default unnamed vectors only. Named-vector collections would
        # require us to know the vector name; out of scope for v0.1.
        vector_params = info.config.params.vectors
        if not isinstance(vector_params, VectorParams):
            raise UpstreamUnavailable(
                f"qdrant collection '{self.cfg.collection}' uses named vectors; "
                f"ingstr expects default unnamed vectors"
            )
        if vector_params.size != expected_vector_dim:
            raise UpstreamUnavailable(
                f"qdrant collection '{self.cfg.collection}' vector size "
                f"{vector_params.size} != configured vector_dim {expected_vector_dim}"
            )

    def upsert_points(self, points: list[QdrantPoint]) -> None:
        """Upsert points in batches of cfg.upsert_batch_size; idempotent on re-run."""
        if not points:
            return
        for start in range(0, len(points), self.cfg.upsert_batch_size):
            batch = points[start : start + self.cfg.upsert_batch_size]
            structs = [
                PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in batch
            ]
            try:
                self._client.upsert(
                    collection_name=self.cfg.collection,
                    points=structs,
                    wait=True,
                )
            except Exception as e:
                raise UpstreamUnavailable(
                    f"qdrant upsert failed (collection={self.cfg.collection}, "
                    f"batch_size={len(batch)}): {e}"
                ) from e

    def delete_points_by_source_path(self, source_path: str) -> None:
        """Delete all points whose payload.source_path equals the given absolute path."""
        try:
            self._client.delete(
                collection_name=self.cfg.collection,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="source_path",
                            match=MatchValue(value=source_path),
                        )
                    ]
                ),
                wait=True,
            )
        except Exception as e:
            raise UpstreamUnavailable(
                f"qdrant delete failed (source_path={source_path}): {e}"
            ) from e

    def set_classification_group(
        self,
        source_path: str,
        new_group: str,
        *,
        indexed_at: str,
    ) -> None:
        """Refresh the `classification_group` (and `indexed_at`) on every point
        whose `source_path` matches. Used in full mode when the file's filesystem
        GID has changed but its content hash hasn't — avoids re-embedding.
        """
        try:
            self._client.set_payload(
                collection_name=self.cfg.collection,
                payload={
                    "classification_group": new_group,
                    "indexed_at": indexed_at,
                },
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="source_path",
                            match=MatchValue(value=source_path),
                        )
                    ]
                ),
                wait=True,
            )
        except Exception as e:
            raise UpstreamUnavailable(
                f"qdrant set_payload failed (source_path={source_path}): {e}"
            ) from e

    def health(self) -> bool:
        """True iff the qdrant endpoint responds. Never raises."""
        try:
            self._client.get_collections()
            return True
        except Exception:
            return False

    def count_points(self) -> int:
        """Exact point count in the configured collection."""
        try:
            result = self._client.count(
                collection_name=self.cfg.collection,
                exact=True,
            )
        except Exception as e:
            raise UpstreamUnavailable(
                f"qdrant count failed (collection={self.cfg.collection}): {e}"
            ) from e
        return result.count

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "QdrantWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
