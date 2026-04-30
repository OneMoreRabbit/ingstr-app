from typing import Any
from unittest.mock import MagicMock

import pytest
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.http.models import Distance, VectorParams

from ingstr.config import QdrantConfig
from ingstr.exceptions import UpstreamUnavailable
from ingstr.qdrant_io import QdrantPoint, QdrantWriter


def _cfg(**overrides: Any) -> QdrantConfig:
    base: dict[str, Any] = dict(
        url="http://qdrant:6333",
        api_key_env="QDRANT_RW_API_KEY",
        collection="documents",
        upsert_batch_size=2,
        timeout_seconds=5,
    )
    base.update(overrides)
    return QdrantConfig(**base)


def _writer(client: MagicMock | None = None) -> QdrantWriter:
    return QdrantWriter(_cfg(), api_key="test-key", client=client or MagicMock())


def _collection_info(*, vector_size: int = 4) -> MagicMock:
    info = MagicMock()
    info.config.params.vectors = VectorParams(size=vector_size, distance=Distance.COSINE)
    return info


def _point(idx: int = 0) -> QdrantPoint:
    return QdrantPoint(
        id=f"id-{idx}",
        vector=[0.0, 1.0, 2.0, 3.0],
        payload={"text": f"chunk {idx}", "chunk_index": idx},
    )


# ── verify_collection ────────────────────────────────────────────────


def test_verify_collection_happy_path() -> None:
    client = MagicMock()
    client.get_collection.return_value = _collection_info(vector_size=4)
    _writer(client).verify_collection(expected_vector_dim=4)
    client.get_collection.assert_called_once_with("documents")


def test_verify_collection_404_raises_with_helpful_message() -> None:
    client = MagicMock()
    client.get_collection.side_effect = UnexpectedResponse(
        status_code=404,
        reason_phrase="Not Found",
        content=b"",
        headers=None,
    )
    with pytest.raises(UpstreamUnavailable, match="does not exist"):
        _writer(client).verify_collection(expected_vector_dim=4)


def test_verify_collection_other_unexpected_response_raises() -> None:
    client = MagicMock()
    client.get_collection.side_effect = UnexpectedResponse(
        status_code=403,
        reason_phrase="Forbidden",
        content=b"",
        headers=None,
    )
    with pytest.raises(UpstreamUnavailable, match="get_collection"):
        _writer(client).verify_collection(expected_vector_dim=4)


def test_verify_collection_network_failure_raises() -> None:
    client = MagicMock()
    client.get_collection.side_effect = ConnectionError("connection refused")
    with pytest.raises(UpstreamUnavailable, match="unreachable"):
        _writer(client).verify_collection(expected_vector_dim=4)


def test_verify_collection_dim_mismatch_raises() -> None:
    client = MagicMock()
    client.get_collection.return_value = _collection_info(vector_size=768)
    with pytest.raises(UpstreamUnavailable, match=r"vector size 768 != configured vector_dim 4"):
        _writer(client).verify_collection(expected_vector_dim=4)


def test_verify_collection_named_vectors_rejected() -> None:
    client = MagicMock()
    info = MagicMock()
    info.config.params.vectors = {"text": VectorParams(size=4, distance=Distance.COSINE)}
    client.get_collection.return_value = info
    with pytest.raises(UpstreamUnavailable, match="named vectors"):
        _writer(client).verify_collection(expected_vector_dim=4)


# ── upsert_points ───────────────────────────────────────────────────────────


def test_upsert_empty_does_not_call_client() -> None:
    client = MagicMock()
    _writer(client).upsert_points([])
    client.upsert.assert_not_called()


def test_upsert_batches_at_configured_size() -> None:
    client = MagicMock()
    # cfg.upsert_batch_size=2; sending 5 → 3 calls (2, 2, 1)
    _writer(client).upsert_points([_point(i) for i in range(5)])
    assert client.upsert.call_count == 3
    batch_sizes = [len(call.kwargs["points"]) for call in client.upsert.call_args_list]
    assert batch_sizes == [2, 2, 1]


def test_upsert_passes_id_vector_payload_through() -> None:
    client = MagicMock()
    p = QdrantPoint(id="abc", vector=[1.0, 2.0, 3.0, 4.0], payload={"text": "x"})
    _writer(client).upsert_points([p])

    call = client.upsert.call_args
    assert call.kwargs["collection_name"] == "documents"
    assert call.kwargs["wait"] is True
    structs = call.kwargs["points"]
    assert len(structs) == 1
    assert structs[0].id == "abc"
    assert structs[0].vector == [1.0, 2.0, 3.0, 4.0]
    assert structs[0].payload == {"text": "x"}


def test_upsert_failure_raises_upstream_unavailable() -> None:
    client = MagicMock()
    client.upsert.side_effect = ConnectionError("refused")
    with pytest.raises(UpstreamUnavailable, match="upsert failed"):
        _writer(client).upsert_points([_point()])


# ── delete_points_by_source_path ────────────────────────────────────────────


def test_delete_uses_filter_on_source_path() -> None:
    client = MagicMock()
    _writer(client).delete_points_by_source_path("/mnt/raid/a.pdf")

    call = client.delete.call_args
    assert call.kwargs["collection_name"] == "documents"
    assert call.kwargs["wait"] is True
    selector = call.kwargs["points_selector"]
    assert len(selector.must) == 1
    cond = selector.must[0]
    assert cond.key == "source_path"
    assert cond.match.value == "/mnt/raid/a.pdf"


def test_delete_failure_raises() -> None:
    client = MagicMock()
    client.delete.side_effect = ConnectionError("refused")
    with pytest.raises(UpstreamUnavailable, match="delete failed"):
        _writer(client).delete_points_by_source_path("/x")


# ── health ──────────────────────────────────────────────────────────────────


def test_health_true_when_get_collections_succeeds() -> None:
    client = MagicMock()
    client.get_collections.return_value = MagicMock()
    assert _writer(client).health() is True


def test_health_false_on_any_failure() -> None:
    client = MagicMock()
    client.get_collections.side_effect = ConnectionError("refused")
    assert _writer(client).health() is False


def test_health_does_not_raise_on_unexpected_response() -> None:
    client = MagicMock()
    client.get_collections.side_effect = UnexpectedResponse(
        status_code=500, reason_phrase="boom", content=b"", headers=None
    )
    assert _writer(client).health() is False


# ── count_points ────────────────────────────────────────────────────────────


def test_count_points_returns_int() -> None:
    client = MagicMock()
    result = MagicMock()
    result.count = 42
    client.count.return_value = result
    assert _writer(client).count_points() == 42

    call = client.count.call_args
    assert call.kwargs["collection_name"] == "documents"
    assert call.kwargs["exact"] is True


def test_count_points_failure_raises() -> None:
    client = MagicMock()
    client.count.side_effect = ConnectionError("refused")
    with pytest.raises(UpstreamUnavailable, match="count failed"):
        _writer(client).count_points()


# ── lifecycle ───────────────────────────────────────────────────────────────


def test_context_manager_calls_close() -> None:
    client = MagicMock()
    with QdrantWriter(_cfg(), api_key="test", client=client):
        pass
    client.close.assert_called_once()


def test_close_tolerates_client_without_close() -> None:
    client = MagicMock(spec=["get_collection", "upsert", "delete", "get_collections", "count"])
    # No close attribute on this mock; should not raise.
    QdrantWriter(_cfg(), api_key="test", client=client).close()
