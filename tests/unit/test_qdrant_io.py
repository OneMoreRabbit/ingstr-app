from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest
from qdrant_client import QdrantClient
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


def _autospec_client() -> Any:
    """A client mock that enforces `QdrantClient`'s real method signatures.

    A bare `MagicMock` accepts **any** keyword, which is why the suite could not
    see that `set_classification_group` passed its filter as `points_selector=`
    when `set_payload` names that parameter `points` (only `delete` takes
    `points_selector`). The call was wrong in a way that raises `TypeError`
    against the real client, and green against a permissive mock — on the RBAC
    reclassification path.

    `create_autospec` closes that hole: a wrong keyword fails here the way it
    would in production. Prefer it over `MagicMock` for anything asserting how we
    *call* the client, as opposed to how we handle what it returns.
    """
    return create_autospec(QdrantClient, instance=True)


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


# ── set_classification_group ────────────────────────────────────────────────
#
# Previously untested: qdrant_io.py:149-171 sat at 0% coverage while the method
# contained a call that could never succeed. These use _autospec_client() so the
# client's real signatures are enforced — see that helper for why.


def test_set_classification_group_calls_set_payload_with_a_valid_signature() -> None:
    """The regression guard for the `points_selector=` / `points=` bug.

    This fails with `TypeError: got an unexpected keyword argument
    'points_selector'` if the old call is restored, because the autospec mock
    enforces `QdrantClient.set_payload`'s real parameter names. The same test
    against a bare `MagicMock` passes either way, which is exactly how the bug
    survived.
    """
    client = _autospec_client()
    _writer(client).set_classification_group(
        "/mnt/raid_arc/drive/a.pdf",
        "arc_g18_any_global",
        indexed_at="2026-09-13T00:00:00Z",
    )

    client.set_payload.assert_called_once()
    call = client.set_payload.call_args
    assert call.kwargs["collection_name"] == "documents"
    assert call.kwargs["payload"] == {
        "classification_group": "arc_g18_any_global",
        "indexed_at": "2026-09-13T00:00:00Z",
    }
    # `points`, not `points_selector` — the distinction the bug turned on.
    assert "points_selector" not in call.kwargs
    assert call.kwargs["points"] is not None
    assert call.kwargs["wait"] is True


def test_set_classification_group_filters_on_the_requested_source_path() -> None:
    """The payload refresh must be scoped to one file.

    An unscoped filter would relabel every point in the collection with one
    agent's group — a silent, collection-wide RBAC change rather than an error.
    """
    client = _autospec_client()
    _writer(client).set_classification_group(
        "/mnt/raid_arc/drive/only-this-one.pdf",
        "arc_g0_engineering_global",
        indexed_at="2026-09-13T00:00:00Z",
    )

    sel = client.set_payload.call_args.kwargs["points"]
    conditions = sel.must
    assert len(conditions) == 1
    assert conditions[0].key == "source_path"
    assert conditions[0].match.value == "/mnt/raid_arc/drive/only-this-one.pdf"


def test_set_classification_group_failure_is_wrapped() -> None:
    client = _autospec_client()
    client.set_payload.side_effect = ConnectionError("refused")
    with pytest.raises(UpstreamUnavailable):
        _writer(client).set_classification_group(
            "/mnt/raid_arc/drive/a.pdf",
            "arc_g18_any_global",
            indexed_at="2026-09-13T00:00:00Z",
        )


def test_delete_still_uses_points_selector_not_points() -> None:
    """The asymmetry that caused the bug, pinned so nobody "fixes" it.

    `delete` genuinely takes `points_selector`; `set_payload` genuinely takes
    `points`. Renaming either to match the other reintroduces the fault.
    """
    client = _autospec_client()
    _writer(client).delete_points_by_source_path("/mnt/raid_arc/drive/gone.pdf")

    call = client.delete.call_args
    assert "points_selector" in call.kwargs
    assert "points" not in call.kwargs
