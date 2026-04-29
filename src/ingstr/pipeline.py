from dataclasses import dataclass
from pathlib import Path

from .config import IngstrConfig
from .embed import EmbeddingClient
from .plan import ResolvedPlan
from .qdrant_io import QdrantWriter
from .state import StateDB


@dataclass
class RunSummary:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_errored: int = 0
    chunks_written: int = 0


def run_ingest(
    cfg: IngstrConfig,
    plan: ResolvedPlan,
    *,
    state: StateDB,
    embedder: EmbeddingClient,
    qdrant: QdrantWriter,
    full: bool,
    dry_run: bool,
) -> RunSummary:
    """Walk source.root and run the per-file pipeline (brief §8).

    Per-file errors are recorded against the file in state and counted but do
    not abort the run. Systemic errors (Qdrant unreachable, plan unreadable)
    propagate up.

    Steps per file: classify → parse → chunk → hash → embed → build points →
    delete old points → upsert → update state. If `full=True`, also delete
    Qdrant points + state rows for files no longer present on disk.
    """
    raise NotImplementedError("run_ingest: orchestrate per brief §8")


def iter_source_files(root: Path, follow_symlinks: bool, exclude_patterns: list[str]):
    """Yield candidate file paths under root, applying exclude_patterns (gitignore-style)."""
    raise NotImplementedError("iter_source_files")
