from pathlib import Path

import pytest


@pytest.fixture
def tmp_yaml(tmp_path: Path):
    """Helper: write a YAML file under tmp_path and return its path."""
    def _write(name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content)
        return p
    return _write
