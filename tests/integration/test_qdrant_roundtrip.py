import pytest

pytestmark = pytest.mark.integration


def test_qdrant_roundtrip_placeholder():
    """Placeholder for the brief §13 integration test.

    Spin up Qdrant via testcontainers, run a full ingest of fixtures/, and
    verify points exist with expected payloads. Implement once embed/qdrant_io
    are in.
    """
    pytest.skip("not yet implemented; awaiting embed.py + qdrant_io.py")
