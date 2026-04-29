import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from ingstr.config import EmbeddingConfig
from ingstr.embed import EmbeddingClient
from ingstr.exceptions import UpstreamUnavailable


def _cfg(**overrides: Any) -> EmbeddingConfig:
    base: dict[str, Any] = dict(
        endpoint="http://ollama:11434",
        model="nomic-embed-text",
        vector_dim=4,
        timeout_seconds=5,
        batch_size=2,
    )
    base.update(overrides)
    return EmbeddingConfig(**base)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    cfg: EmbeddingConfig | None = None,
) -> EmbeddingClient:
    return EmbeddingClient(cfg or _cfg(), transport=httpx.MockTransport(handler))


# ── embed() ─────────────────────────────────────────────────────────────────


def test_empty_input_returns_empty_list_without_calling_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("endpoint should not be called for empty input")

    with _client(handler) as c:
        assert c.embed([]) == []


def test_single_batch_returns_vectors_in_input_order() -> None:
    requests_seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests_seen.append(body["input"])
        return httpx.Response(
            200, json={"embeddings": [[0.0, 1.0, 2.0, 3.0], [4.0, 5.0, 6.0, 7.0]]}
        )

    with _client(handler) as c:
        result = c.embed(["hello", "world"])

    assert result == [[0.0, 1.0, 2.0, 3.0], [4.0, 5.0, 6.0, 7.0]]
    assert requests_seen == [["hello", "world"]]


def test_request_uses_configured_model_and_path() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3, 0.4]]})

    with _client(handler) as c:
        c.embed(["x"])

    assert captured["url"].endswith("/api/embed")
    assert captured["body"]["model"] == "nomic-embed-text"
    assert captured["body"]["input"] == ["x"]


def test_multiple_batches_respect_batch_size() -> None:
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)["input"]
        seen.append(batch)
        return httpx.Response(
            200,
            json={"embeddings": [[float(i)] * 4 for i in range(len(batch))]},
        )

    # batch_size=2, sending 5 inputs → 3 batches of 2, 2, 1
    with _client(handler) as c:
        result = c.embed(["a", "b", "c", "d", "e"])

    assert len(result) == 5
    assert [len(b) for b in seen] == [2, 2, 1]
    assert seen[0] == ["a", "b"]
    assert seen[2] == ["e"]


def test_dimension_mismatch_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[1.0, 2.0]]})  # dim=2, expected 4

    with _client(handler) as c, pytest.raises(UpstreamUnavailable, match="dimension mismatch"):
        c.embed(["hello"])


def test_wrong_vector_count_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[1.0, 2.0, 3.0, 4.0]]})  # 1 vec, 2 inputs

    with _client(handler) as c, pytest.raises(
        UpstreamUnavailable, match="returned 1 vectors for 2"
    ):
        c.embed(["hello", "world"])


def test_missing_embeddings_key_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with _client(handler) as c, pytest.raises(UpstreamUnavailable, match="returned no vectors"):
        c.embed(["hello"])


def test_http_5xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    with _client(handler) as c, pytest.raises(UpstreamUnavailable, match="failed"):
        c.embed(["hello"])


def test_connection_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _client(handler) as c, pytest.raises(UpstreamUnavailable, match="failed"):
        c.embed(["hello"])


def test_malformed_json_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not valid json", headers={"content-type": "application/json"})

    with _client(handler) as c, pytest.raises(UpstreamUnavailable, match="failed"):
        c.embed(["hello"])


# ── health() ────────────────────────────────────────────────────────────────


def test_health_true_when_configured_model_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"models": [{"name": "nomic-embed-text:latest"}, {"name": "llama3:8b"}]}
        )

    with _client(handler) as c:
        assert c.health() is True


def test_health_matches_on_base_name_ignoring_tag() -> None:
    cfg = _cfg(model="nomic-embed-text:latest")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "nomic-embed-text:v1.5"}]})

    with _client(handler, cfg=cfg) as c:
        assert c.health() is True


def test_health_false_when_model_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})

    with _client(handler) as c:
        assert c.health() is False


def test_health_false_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with _client(handler) as c:
        assert c.health() is False


def test_health_false_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with _client(handler) as c:
        assert c.health() is False


def test_health_false_on_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": "not-a-list"})

    with _client(handler) as c:
        assert c.health() is False


def test_health_uses_tags_endpoint() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"models": []})

    with _client(handler) as c:
        c.health()

    assert captured["path"] == "/api/tags"


# ── lifecycle ───────────────────────────────────────────────────────────────


def test_context_manager_closes_underlying_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": []})

    c = EmbeddingClient(_cfg(), transport=httpx.MockTransport(handler))
    with c:
        pass
    assert c._client.is_closed
