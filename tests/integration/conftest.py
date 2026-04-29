import pytest

# Integration tests require external services (Qdrant, Ollama). They are
# marked `integration` and skipped unless explicitly selected:
#     pytest -m integration
collect_ignore_glob = []


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("-m", default="") and "integration" in config.getoption("-m"):
        return
    skip_integration = pytest.mark.skip(reason="integration tests require -m integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
