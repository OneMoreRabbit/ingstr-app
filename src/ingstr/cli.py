import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import structlog
import typer

from . import __version__
from .config import DEFAULT_CONFIG_PATH, IngstrConfig, OrgView, load_config
from .embed import EmbeddingClient
from .exceptions import ConfigError, PlanError, UpstreamUnavailable
from .logging_setup import configure_logging
from .pipeline import RunSummary, run_ingest
from .plan import ResolvedPlan, load_plan
from .qdrant_io import QdrantWriter
from .state import RunRecord, StateDB

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

OrgOption = Annotated[
    str | None,
    typer.Option(
        "--org",
        help=(
            "Which organisation to act on. Required when the configuration "
            "lists more than one; with exactly one it may be omitted."
        ),
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
    org: OrgOption = None,
    full: Annotated[bool, typer.Option("--full", help="Force a complete re-walk.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan but do not write.")] = False,
    expect_removals: Annotated[
        int | None,
        typer.Option(
            "--expect-removals",
            help=(
                "Allow this many FILES to have their passages removed in one "
                "run, when the guard would otherwise refuse. The number is "
                "files, and it is the number a --dry-run prints. Never set "
                "this in a timer unit: it is typed by hand, for one run, by "
                "someone who has looked."
            ),
        ),
    ] = None,
) -> None:
    """Walk one org's tree and ingest changed files."""
    whole = _load_or_exit(config)
    cfg = _org_or_exit(whole, org)
    configure_logging(cfg.logging)
    log = structlog.get_logger("ingstr.ingest").bind(org=cfg.org)

    plan = _load_plan_or_exit(cfg)
    _require_plan_path_if_granting(cfg, plan)
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
                    expect_removals=expect_removals,
                )
            except UpstreamUnavailable as e:
                exit_code = _EXIT_UPSTREAM
                _finalise_run(
                    state, run_id, started_at=started_at, summary=None, exit_code=exit_code
                )
                typer.echo(f"upstream unavailable: {e}", err=True)
                raise typer.Exit(code=_EXIT_UPSTREAM) from e
            except Exception as e:
                exit_code = _EXIT_FATAL
                _finalise_run(
                    state, run_id, started_at=started_at, summary=None, exit_code=exit_code
                )
                log.exception("run_aborted_unexpected")
                typer.echo(f"fatal: {e}", err=True)
                raise typer.Exit(code=_EXIT_FATAL) from e

            # A refused purge is a partial run: everything ingested stands, the
            # deletions were withheld. ADR-0010 §6 requires it be non-zero —
            # exiting 0 here would let a broken mount read as a quiet day, which
            # is the failure the guard exists to make loud.
            if dry_run and (
                summary.passages_removed_gone
                or summary.passages_removed_lost_access
            ):
                # Counts only, never paths: a path is org data and this is
                # otter's journal. This is where the number for
                # --expect-removals comes from.
                typer.echo(
                    f"would remove passages for "
                    f"{summary.passages_removed_gone} file(s) gone from disk "
                    f"and {summary.passages_removed_lost_access} file(s) that "
                    "can no longer be classified"
                )
            if summary.purge_refused_reason is not None:
                typer.echo(summary.purge_refused_reason, err=True)
                typer.echo(
                    f"nothing was removed: {summary.passages_removed_gone} "
                    f"file(s) gone from disk and "
                    f"{summary.passages_removed_lost_access} file(s) that can "
                    "no longer be classified were left in place. Until this "
                    "is cleared, passages of files whose access was taken "
                    "away can still be found. Run with --dry-run to see the "
                    "counts, then --expect-removals <n> to allow it.",
                    err=True,
                )
                exit_code = _EXIT_PARTIAL
            else:
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
def stats(config: ConfigOption = DEFAULT_CONFIG_PATH, org: OrgOption = None) -> None:
    """Print one org's file/chunk/error counts and last-run timestamps. Read-only."""
    whole = _load_or_exit(config)
    cfg = _org_or_exit(whole, org)
    configure_logging(cfg.logging)

    api_key = os.environ.get(cfg.qdrant.api_key_env, "")

    state = StateDB(cfg.state.db_path)
    try:
        files_known = len(state.all_paths())
        files_errored = state.count_errored_files()
        last_success = state.get_last_run(success_only=True)
        last_incremental = state.get_last_run(mode="incremental")
        last_full = state.get_last_run(mode="full")
        refused = state.outstanding_refused_removal()
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
    if refused is not None:
        # A refused removal is a STATE, not a moment: until it is cleared,
        # passages of files whose access was taken away can still be found.
        # Printed here because this is where someone comes to ask whether it
        # is still true, after the alert and after the journal has rolled —
        # and NOT printed once a later run has done the removal, because a
        # warning that never clears is one nobody reads.
        typer.echo(
            f"REMOVAL REFUSED:    run {refused.run_id} at {refused.finished_at}"
        )
        typer.echo(
            f"                    {refused.gone_count or 0} file(s) gone from "
            f"disk, {refused.lost_access_count or 0} file(s) that can no "
            "longer be classified"
        )
        typer.echo(
            "                    until this is cleared, passages of files whose "
            "access was taken away can still be found"
        )
        typer.echo(f"                    {refused.purge_refused_reason}")
    typer.echo(f"last successful:    {_run_summary(last_success)}")
    typer.echo(f"last incremental:   {_run_summary(last_incremental)}")
    typer.echo(f"last full:          {_run_summary(last_full)}")


@app.command()
def health(config: ConfigOption = DEFAULT_CONFIG_PATH, org: OrgOption = None) -> None:
    """Check one org's Qdrant, Ollama, source mount and the compiled plan."""
    whole = _load_or_exit(config)
    cfg = _org_or_exit(whole, org)
    configure_logging(cfg.logging)

    checks: list[tuple[str, bool, str]] = []
    checks.append(("org", True, cfg.org))

    src_ok = cfg.source.root.is_dir()
    checks.append(("source.root", src_ok, str(cfg.source.root)))

    try:
        plan = load_plan(cfg.plan.compiled_plan_path, cfg.plan.group_gid_map_path)
        checks.append(("plan", True, f"{len(plan.gid_to_group)} groups"))
        # A plan without `groups[]` never reaches here — load_plan raises, and
        # the check above reports it. What is worth reporting here is a plan
        # that parses and gives this org nothing, which reads as a quiet
        # success otherwise.
        if plan.grants:
            # A WRONG plan_path is silent: it matches no grant, and every
            # granted file reads as unclassifiable exactly as it did before.
            # So report how many of the plan's granted folders actually lie
            # under the declared path. Zero, with grants present, is the
            # signature of a wrong value — the one thing nothing else says.
            under = (
                [p for p in plan.grants if p.startswith(cfg.source.plan_path)]
                if cfg.source.plan_path is not None
                else []
            )
            if cfg.source.plan_path is None:
                detail = (
                    f"{len(plan.grants)} granted folder(s); source.plan_path is "
                    "not declared — no granted file can be matched"
                )
            elif not under:
                detail = (
                    f"NONE of the plan's {len(plan.grants)} granted folder(s) "
                    f"lie under source.plan_path={cfg.source.plan_path!r} — "
                    "the value is probably wrong, and a wrong one matches "
                    "nothing silently"
                )
            else:
                detail = (
                    f"{len(under)} of {len(plan.grants)} granted folder(s) lie "
                    f"under source.plan_path={cfg.source.plan_path!r}"
                )
            checks.append(("plan.grants", bool(under), detail))
        org_gids = plan.gids_for_org(cfg.org)
        checks.append(
            (
                "plan.groups",
                bool(org_gids),
                f"{len(org_gids)} group(s) for org {cfg.org!r}"
                + ("" if org_gids else " — this run would read nothing"),
            )
        )
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


def _org_or_exit(cfg: IngstrConfig, org: str | None) -> OrgView:
    """Resolve the org to act on, or exit 1 naming the configured ones.

    The org decides which folder is read and which collection is written, so
    with several configured it is declared or the tool stops (constitution
    §11). Picking the first would be a plausible default for a target, which
    is the kind nobody audits.
    """
    try:
        return cfg.for_org(org)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(code=_EXIT_CONFIG) from e


def _load_or_exit(config_path: Path) -> IngstrConfig:
    """Load config, mapping known errors to brief §4 exit codes."""
    try:
        return load_config(config_path)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(code=_EXIT_CONFIG) from e


def _require_plan_path_if_granting(cfg: OrgView, plan: ResolvedPlan) -> None:
    """Stop before the walk if the plan grants and nothing says where we are.

    The plan writes its grant paths relative to a root on beaver; this
    container sees its own mount point. Without `source.plan_path` no file can
    be matched against any grant — and the failure is not an error, it is
    SILENCE: every granted file simply reads as unclassifiable, the run exits
    3, and a folder the operator believed he had shared stays unsearchable
    with nothing saying why.

    So the run stops, and says what to add (constitution §11).
    """
    if plan.grants and cfg.source.plan_path is None:
        granted = ", ".join(sorted(plan.grants)[:3])
        typer.echo(
            f"config error: the plan grants access to {len(plan.grants)} "
            f"folder(s) (for example {granted}) and this org does not declare "
            "source.plan_path, so none of their files can be matched. Add "
            "source.plan_path to this org: where its source.root sits beneath "
            "the plan's acl root, written as the plan writes paths "
            "(e.g. 'arc/drive/').",
            err=True,
        )
        raise typer.Exit(code=_EXIT_CONFIG)


def _load_plan_or_exit(cfg: OrgView) -> ResolvedPlan:
    try:
        return load_plan(cfg.plan.compiled_plan_path, cfg.plan.group_gid_map_path)
    except PlanError as e:
        typer.echo(f"plan error: {e}", err=True)
        raise typer.Exit(code=_EXIT_UPSTREAM) from e


def _read_api_key_or_exit(cfg: OrgView) -> str:
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
        purge_refused_reason=summary.purge_refused_reason if summary else None,
        reached_removal_step=summary.reached_removal_step if summary else False,
        gone_count=summary.passages_removed_gone if summary else 0,
        lost_access_count=summary.passages_removed_lost_access if summary else 0,
    )


def _run_summary(run: RunRecord | None) -> str:
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
    return datetime.now(UTC).isoformat()


def main() -> None:
    if os.name == "nt":
        sys.stderr.write(
            "ingstr: Windows is not a supported runtime; targets Linux only.\n"
        )
        sys.exit(1)
    app()


if __name__ == "__main__":
    main()
