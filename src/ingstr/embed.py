from .config import EmbeddingConfig


class EmbeddingClient:
    """Synchronous httpx client for an Ollama-compatible embedding endpoint.

    Batches `cfg.batch_size` chunks per request. Validates that returned vectors
    match `cfg.vector_dim` — a mismatch raises so we never write the wrong shape
    into Qdrant.
    """

    def __init__(self, cfg: EmbeddingConfig) -> None:
        self.cfg = cfg

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("EmbeddingClient.embed: POST to {endpoint}/api/embed in batches")

    def health(self) -> bool:
        """Return True if the endpoint responds within timeout, else False."""
        raise NotImplementedError("EmbeddingClient.health")

    def close(self) -> None:
        raise NotImplementedError("EmbeddingClient.close")
