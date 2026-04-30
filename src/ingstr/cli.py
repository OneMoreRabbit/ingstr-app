import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import structlog
import typer

from . import __version__
from .config import DEFAULT_CONFIG_PATH, IngstrConfig, load_config
from .embed import EmbeddingClient
from .exceptions import ConfigError, PlanError, UpstreamUnavailable
from .logging_setup import configure_logging
from .pipeline import RunSummary, run_ingest
from .plan import load_plan
from .qdrant_io import QdrantWriter
from .state import StateDB

app = typer.Typer(
    name="ingstr",
    help="Document ingestion pipeline for Qdrant.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config",
        "-c",
        help="Path to Ingstr config YAML.",
        envvar="INGSTR_CONFIG",
    ),
]


# Exit codes per brief §4
_EXIT_OK = 0
_EXIT_CONFIG = 1
_EXIT_UPSTREAM = 2
_EXIT_PARTIAL = 3
_EXIT_FATAL = 4


@app.command()
def ingest(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    full: Annotated[bool, typer.Option("--full", help="Force a complete re-walk.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan but do not write.")] = False,
) -> None:
    """Walk the configured tree and ingest changed files."""
    cfg = _load_or_exit(config)
    configure_logging(cfg.logging)
    log = structlog.get_logger("ingstr.ingest")

    plan = _load_plan_or_exit(cfg)
    api_key = _read_api_key_or_exit(cfg)

    mode = "full" if full else "incremental"
    started_at = _utc_now_iso()

    state = StateDB(cfg.state.db_path)
    run_id: int | None = None
    exit_code = _EXIT_OK

    try:
        with EmbeddingClient(cfg.embedding) as embedder, QdrantWriter(
            cfg.qdrant, api_key
        ) as qdrant:
            try:
                qdrant.verify_collection(cfg.embedding.vector_dim)
            except UpstreamUnavailable as e:
                typer.echo(f"upstream unavailable: {e}", err=True)
                raise typer.Exit(code=_EXIT_UPSTREAM) from e

            run_id = state.start_run(mode=mode, started_at=started_at)
            log.info("run_started", run_id=run_id, mode=mode, dry_run=dry_run)

            try:
                summary = run_ingest(
                    cfg,
                    plan,
                    state=state,
                    embedder=embedder,
                    qdrant=qdrant,
                    full=full,
                    dry_run=dry_run,
                )
            except UpstreamUnavailable as e:
                exit_code = _EXIT_UPSTREAM
                _finalise_run(state, run_id, started_at=started_at, summary=None, exit_code=exit_code)
                typer.echo(f"upstream unavailable: {e}", err=True)
                raise typer.Exit(code=_EXIT_UPSTREAM) from e
            except Exception as e:
                exit_code = _EXIT_FATAL
                _finalise_run(state, run_id, started_at=started_at, summary=None, exit_code=exit_code)
                log.exception("run_aborted_unexpected")
                typer.echo(f"fatal: {e}", err=True)
                raise typer.Exit(code=_EXIT_FATAL) from e

            exit_code = _EXIT_PARTIAL if summary.files_errored > 0 else _EXIT_OK
            _finalise_run(
                state, run_id, started_at=started_at, summary=summary, exit_code=exit_code
            )
            log.info(
                "run_finished",
                run_id=run_id,
                files_seen=summary.files_seen,
                files_indexed=summary.files_indexed,
                files_skipped=summary.files_skipped,
                files_errored=summary.files_errored,
                files_deleted=summary.files_deleted,
                chunks_written=summary.chunks_written,
                exit_code=exit_code,
            )
    finally:
        state.close()

    if exit_code != _EXIT_OK:
        raise typer.Exit(code=exit_code)


@app.command()
def stats(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Print file/chunk/error counts and last-run timestamps. Read-only."""
    cfg = _load_or_exit(config)
    configure_logging(cfg.logging)

    api_key = os.environ.get(cfg.qdrant.api_key_env, "")

    state = StateDB(cfg.state.db_path)
    try:
        files_known = len(state.all_paths())
        files_errored = state.count_errored_files()
        last_success = state.get_last_run(success_only=True)
        last_incremental = state.get_last_run(mode="incremental")
        last_full = state.get_last_run(mode="full")
    finally:
        state.close()

    chunks_stored: int | str
    try:
        with QdrantWriter(cfg.qdrant, api_key) as qdrant:
            chunks_stored = qdrant.count_points()
    except UpstreamUnavailable as e:
        chunks_stored = f"<qdrant unreachable: {e}>"

    typer.echo(f"files known:        {files_known}")
    typer.echo(f"files errored:      {files_errored}")
    typer.echo(f"chunks stored:      {chunks_stored}")
    typer.echo(f"last successful:    {_run_summary(last_success)}")
    typer.echo(f"last incremental:   {_run_summary(last_incremental)}")
    typer.echo(f"last full:          {_run_summary(last_full)}")


@app.command()
def health(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Check connectivity to Qdrant, Ollama, NFS mount, and the compiled plan."""
    cfg = _load_or_exit(config)
    configure_logging(cfg.logging)

    checks: list[tuple[str, bool, str]] = []

    src_ok = cfg.source.root.is_dir()
    checks.append(("source.root", src_ok, str(cfg.source.root)))

    try:
        plan = load_plan(cfg.plan.compiled_plan_path, cfg.plan.group_gid_map_path)
        checks.append(("plan", True, f"{len(plan.gid_to_group)} groups"))
    except PlanError as e:
        checks.append(("plan", False, str(e)))

    try:
        with EmbeddingClient(cfg.embedding) as embedder:
            ollama_ok = embedder.health()
        checks.append(
            (
                "ollama",
                ollama_ok,
                f"{cfg.embedding.endpoint} model={cfg.embedding.model}",
            )
        )
    except Exception as e:
        checks.append(("ollama", False, str(e)))

    api_key = os.environ.get(cfg.qdrant.api_key_env, "")
    try:
        with QdrantWriter(cfg.qdrant, api_key) as qdrant:
            q_ok = qdrant.health()
            checks.append(("qdrant.connect", q_ok, cfg.qdrant.url))
            if q_ok:
                try:
                    qdrant.verify_collection(cfg.embedding.vector_dim)
                    checks.append(
                        (
                            "qdrant.collection",
                            True,
                            f"{cfg.qdrant.collection} (dim={cfg.embedding.vector_dim})",
                        )
                    )
                except UpstreamUnavailable as e:
                    checks.append(("qdrant.collection", False, str(e)))
    except Exception as e:
        checks.append(("qdrant.connect", False, str(e)))

    all_ok = all(ok for _, ok, _ in checks)
    for name, ok, detail in checks:
        marker = "[OK]" if ok else "[FAIL]"
        typer.echo(f"{marker:6s} {name:24s} {detail}")

    raise typer.Exit(code=_EXIT_OK if all_ok else _EXIT_UPSTREAM)


@app.command()
def version() -> None:
    """Print version and exit."""
    typer.echo(__version__)


# ── helpers ─────────────────────────────────────────────────────────────────


def _load_or_exit(config_path: Path) -> IngstrConfig:
    """Load config, mapping known errors to brief §4 exit codes."""
    try:
        return load_config(config_path)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(code=_EXIT_CONFIG) from e


def _load_plan_or_exit(cfg: IngstrConfig):
    try:
        return load_plan(cfg.plan.compiled_plan_path, cfg.plan.group_gid_map_path)
    except PlanError as e:
        typer.echo(f"plan error: {e}", err=True)
        raise typer.Exit(code=_EXIT_UPSTREAM) from e


def _read_api_key_or_exit(cfg: IngstrConfig) -> str:
    api_key = os.environ.get(cfg.qdrant.api_key_env, "")
    if not api_key:
        typer.echo(
            f"config error: env var {cfg.qdrant.api_key_env} (qdrant.api_key_env) is not set",
            err=True,
        )
        raise typer.Exit(code=_EXIT_CONFIG)
    return api_key


def _finalise_run(
    state: StateDB,
    run_id: int,
    *,
    started_at: str,
    summary: RunSummary | None,
    exit_code: int,
) -> None:
    state.finish_run(
        run_id,
        finished_at=_utc_now_iso(),
        files_seen=summary.files_seen if summary else 0,
        files_indexed=summary.files_indexed if summary else 0,
        files_skipped=summary.files_skipped if summary else 0,
        files_errored=summary.files_errored if summary else 0,
        chunks_written=summary.chunks_written if summary else 0,
        exit_code=exit_code,
    )


def _run_summary(run) -> str:
    if run is None:
        return "(none)"
    return (
        f"run #{run.run_id} {run.mode} "
        f"finished_at={run.finished_at} "
        f"indexed={run.files_indexed} skipped={run.files_skipped} "
        f"errored={run.files_errored} chunks={run.chunks_written} "
        f"exit={run.exit_code}"
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    if os.name == "nt":
        sys.stderr.write(
            "ingstr: Windows is not a supported runtime; targets Linux only.\n"
        )
        sys.exit(1)
    app()


if __name__ == "__main__":
    main()
