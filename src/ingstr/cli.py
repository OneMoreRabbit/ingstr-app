import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import DEFAULT_CONFIG_PATH, load_config
from .exceptions import ConfigError, PlanError, UpstreamUnavailable
from .logging_setup import configure_logging

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


@app.command()
def ingest(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    full: Annotated[bool, typer.Option("--full", help="Force a complete re-walk.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan but do not write.")] = False,
) -> None:
    """Walk the configured tree and ingest changed files."""
    cfg = _load_or_exit(config)
    configure_logging(cfg.logging)
    _ = (full, dry_run, cfg)
    raise NotImplementedError("ingest: wire pipeline.run_ingest")


@app.command()
def stats(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Print file/chunk/error counts and last-run timestamps. Read-only."""
    cfg = _load_or_exit(config)
    configure_logging(cfg.logging)
    raise NotImplementedError("stats: read state DB and Qdrant counters")


@app.command()
def health(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Check connectivity to Qdrant, Ollama, NFS mount, and the compiled plan."""
    cfg = _load_or_exit(config)
    configure_logging(cfg.logging)
    raise NotImplementedError("health: check each upstream and exit non-zero on failure")


@app.command()
def version() -> None:
    """Print version and exit."""
    typer.echo(__version__)


def _load_or_exit(config_path: Path) -> "object":  # noqa: UP037
    """Load config, mapping known errors to brief §4 exit codes."""
    try:
        return load_config(config_path)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except PlanError as e:
        typer.echo(f"plan error: {e}", err=True)
        raise typer.Exit(code=2) from e
    except UpstreamUnavailable as e:
        typer.echo(f"upstream unavailable: {e}", err=True)
        raise typer.Exit(code=2) from e


def main() -> None:
    if os.name == "nt":
        sys.stderr.write(
            "ingstr: Windows is not a supported runtime; targets Linux only.\n"
        )
        sys.exit(1)
    app()


if __name__ == "__main__":
    main()
