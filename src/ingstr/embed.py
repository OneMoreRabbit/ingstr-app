import httpx

from .config import EmbeddingConfig
from .exceptions import UpstreamUnavailable

_EMBED_PATH = "/api/embed"
_TAGS_PATH = "/api/tags"
_HEALTH_TIMEOUT = 5.0


class EmbeddingClient:
    """Synchronous HTTP client for an Ollama-compatible embedding endpoint.

    Uses Ollama's batched `/api/embed` (0.2+) so each HTTP round-trip handles
    `cfg.batch_size` chunks. Validates returned vector count and dimensionality
    against the input batch and `cfg.vector_dim`; mismatches raise
    `UpstreamUnavailable` so we never write the wrong shape into Qdrant.
    """

    def __init__(
        self,
        cfg: EmbeddingConfig,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self._client = httpx.Client(
            base_url=cfg.endpoint,
            timeout=cfg.timeout_seconds,
            transport=transport,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in input order."""
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self.cfg.batch_size):
            batch = texts[start : start + self.cfg.batch_size]
            out.extend(self._embed_batch(batch))
        return out

    def health(self) -> bool:
        """True iff the endpoint responds and the configured model is loaded.

        Compares on the model's base name (everything before `:`), so
        `nomic-embed-text` in config matches a pulled `nomic-embed-text:latest`
        on the Ollama side.
        """
        try:
            response = self._client.get(_TAGS_PATH, timeout=_HEALTH_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return False

        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return False

        configured_base = self.cfg.model.split(":", 1)[0]
        for entry in models:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name.split(":", 1)[0] == configured_base:
                return True
        return False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "EmbeddingClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        try:
            response = self._client.post(
                _EMBED_PATH,
                json={"model": self.cfg.model, "input": batch},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise UpstreamUnavailable(
                f"embedding endpoint {self.cfg.endpoint} failed: {e}"
            ) from e

        vectors = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(batch):
            got = len(vectors) if isinstance(vectors, list) else "no"
            raise UpstreamUnavailable(
                f"embedding endpoint returned {got} vectors for {len(batch)} inputs"
            )

        for v in vectors:
            if not isinstance(v, list) or len(v) != self.cfg.vector_dim:
                got = len(v) if isinstance(v, list) else "?"
                raise UpstreamUnavailable(
                    f"embedding dimension mismatch: got {got}, "
                    f"expected {self.cfg.vector_dim} (model={self.cfg.model})"
                )

        return vectors
