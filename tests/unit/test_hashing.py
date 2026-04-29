import hashlib
from pathlib import Path

from ingstr.hashing import sha256_file


def test_sha256_matches_hashlib(tmp_path: Path):
    p = tmp_path / "f.bin"
    payload = b"the quick brown fox jumps over the lazy dog\n" * 1000
    p.write_bytes(payload)
    assert sha256_file(p) == hashlib.sha256(payload).hexdigest()


def test_empty_file_is_sha256_of_empty(tmp_path: Path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert sha256_file(p) == hashlib.sha256(b"").hexdigest()


def test_streaming_handles_larger_than_chunk(tmp_path: Path):
    p = tmp_path / "big.bin"
    payload = b"x" * (4 * 1024 * 1024 + 17)
    p.write_bytes(payload)
    assert sha256_file(p) == hashlib.sha256(payload).hexdigest()
