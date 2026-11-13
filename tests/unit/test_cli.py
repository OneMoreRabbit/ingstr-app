"""Smoke tests for the CLI surface.

These exercise the wiring (subcommands registered, exit codes, config error
mapping). They don't run a real ingest end-to-end — that's pipeline_run's
remit. Uses Typer's built-in CliRunner.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ingstr import __version__
from ingstr.cli import app
from ingstr.pipeline import RunSummary

runner = CliRunner()


_MIN_CFG = """\
orgs:
  - org: test
    source:
      root: {root}
    qdrant:
      url: http://qdrant:6333
      api_key_env: QDRANT_RW_API_KEY
      collection: documents
    state:
      db_path: {db}
plan:
  compiled_plan_path: {plan}
  group_gid_map_path: {gid_map}
embedding:
  endpoint: http://ollama:11434
  model: nomic-embed-text
  vector_dim: 4
"""


def _cfg_file(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    root.mkdir()
    plan = tmp_path / "plan.yml"
    plan.write_text(
        "required_groups:\n- arc_g0_engineering_global\n"
        "groups:\n- {name: arc_g0_engineering_global, org: test, grade: g0,\n"
        "   vertical: engineering, scope: global}\n"
    )
    gid_map = tmp_path / "gid_map.yml"
    gid_map.write_text("groups:\n  arc_g0_engineering_global: 1003\n")
    db = tmp_path / "state.db"
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        _MIN_CFG.format(root=root, plan=plan, gid_map=gid_map, db=db)
    )
    return cfg


# ── version ─────────────────────────────────────────────────────────────────


def test_version_prints_and_exits_zero() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


# ── --help ──────────────────────────────────────────────────────────────────


def test_help_lists_all_four_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("ingest", "stats", "health", "version"):
        assert cmd in result.stdout


# ── config-error path (exit 1) ──────────────────────────────────────────────


def test_missing_config_exits_one(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ingest", "--config", str(tmp_path / "absent.yml")])
    assert result.exit_code == 1
    assert "config error" in result.stderr


def test_invalid_config_exits_one(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yml"
    cfg.write_text("org: test\n  bad: indent: here")
    result = runner.invoke(app, ["ingest", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "config error" in result.stderr


# ── ingest: api key missing → config error ──────────────────────────────────


def test_ingest_missing_api_key_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg_file(tmp_path)
    monkeypatch.delenv("QDRANT_RW_API_KEY", raising=False)
    result = runner.invoke(app, ["ingest", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "QDRANT_RW_API_KEY" in result.stderr


# ── ingest: plan error → upstream (exit 2) ─────────────────────────────────


def test_ingest_plan_load_failure_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg_file(tmp_path)
    # Break the plan file so load_plan raises PlanError
    plan_path = tmp_path / "plan.yml"
    plan_path.write_text("required_groups: not-a-list\n")
    monkeypatch.setenv("QDRANT_RW_API_KEY", "x")
    result = runner.invoke(app, ["ingest", "--config", str(cfg)])
    assert result.exit_code == 2
    assert "plan error" in result.stderr


# ── ingest: dry-run end-to-end with mocks ──────────────────────────────────


def test_ingest_dry_run_with_mocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg_file(tmp_path)
    monkeypatch.setenv("QDRANT_RW_API_KEY", "x")

    # Empty source root → no files seen → exit 0 even with all mocks raising
    with patch("ingstr.cli.EmbeddingClient") as Embed, \
         patch("ingstr.cli.QdrantWriter") as Qdrant:
        Qdrant.return_value.__enter__.return_value.verify_collection.return_value = None
        Embed.return_value.__enter__.return_value.embed.return_value = []
        result = runner.invoke(
            app, ["ingest", "--config", str(cfg), "--dry-run"]
        )

    assert result.exit_code == 0


# ── health: prints checks, exits 0 when all pass ───────────────────────────


def test_health_all_passing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg_file(tmp_path)
    monkeypatch.setenv("QDRANT_RW_API_KEY", "x")

    with patch("ingstr.cli.EmbeddingClient") as Embed, \
         patch("ingstr.cli.QdrantWriter") as Qdrant:
        Embed.return_value.__enter__.return_value.health.return_value = True
        q = Qdrant.return_value.__enter__.return_value
        q.health.return_value = True
        q.verify_collection.return_value = None
        result = runner.invoke(app, ["health", "--config", str(cfg)])

    assert result.exit_code == 0
    assert "[OK]" in result.stdout
    assert "source.root" in result.stdout
    assert "plan" in result.stdout
    assert "ollama" in result.stdout
    assert "qdrant.connect" in result.stdout
    assert "qdrant.collection" in result.stdout


def test_health_failing_check_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg_file(tmp_path)
    monkeypatch.setenv("QDRANT_RW_API_KEY", "x")

    with patch("ingstr.cli.EmbeddingClient") as Embed, \
         patch("ingstr.cli.QdrantWriter") as Qdrant:
        Embed.return_value.__enter__.return_value.health.return_value = False
        Qdrant.return_value.__enter__.return_value.health.return_value = True
        Qdrant.return_value.__enter__.return_value.verify_collection.return_value = None
        result = runner.invoke(app, ["health", "--config", str(cfg)])

    assert result.exit_code == 2
    assert "[FAIL]" in result.stdout


# ── stats: prints, qdrant unreachable doesn't crash ────────────────────────


def test_stats_handles_qdrant_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg_file(tmp_path)
    monkeypatch.setenv("QDRANT_RW_API_KEY", "x")

    with patch("ingstr.cli.QdrantWriter") as Qdrant:
        from ingstr.exceptions import UpstreamUnavailable
        Qdrant.side_effect = UpstreamUnavailable("refused")
        result = runner.invoke(app, ["stats", "--config", str(cfg)])

    assert result.exit_code == 0
    assert "files known:" in result.stdout
    assert "qdrant unreachable" in result.stdout


def test_ingest_exits_non_zero_when_a_purge_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0010 §6: refuse, report, exit non-zero.

    Exiting 0 would let a broken mount read as a quiet day — the guard would
    have done its job and nobody would know, which is the same silent-success
    shape the guard exists to break.
    """
    cfg = _cfg_file(tmp_path)
    monkeypatch.setenv("QDRANT_RW_API_KEY", "x")
    refused = RunSummary(purge_refused_reason="REFUSING to purge: 9 of 10 known files")

    with patch("ingstr.cli.EmbeddingClient") as Embed, \
         patch("ingstr.cli.QdrantWriter") as Qdrant, \
         patch("ingstr.cli.run_ingest", return_value=refused):
        Qdrant.return_value.__enter__.return_value.verify_collection.return_value = None
        Embed.return_value.__enter__.return_value.embed.return_value = []
        result = runner.invoke(app, ["ingest", "--config", str(cfg), "--full"])

    assert result.exit_code == 3
    assert "REFUSING to purge" in result.stderr


# ── v0.2.0: one configuration, every org ────────────────────────────────────


_TWO_ORG_CFG = """\
orgs:
  - org: arc
    source:
      root: {root}
    qdrant:
      url: http://qdrant_arc:6333
      api_key_env: QDRANT_RW_API_KEY
      collection: documents
    state:
      db_path: {db}
  - org: job
    source:
      root: {root}
    qdrant:
      url: http://qdrant_job:6333
      api_key_env: QDRANT_RW_API_KEY
      collection: documents_job
    state:
      db_path: {db}
plan:
  compiled_plan_path: {plan}
  group_gid_map_path: {gid_map}
embedding:
  endpoint: http://ollama:11434
  model: nomic-embed-text
  vector_dim: 4
"""


def _two_org_cfg_file(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    root.mkdir()
    plan = tmp_path / "plan.yml"
    plan.write_text(
        "required_groups:\n- arc_g0_engineering_global\n"
        "groups:\n- {name: arc_g0_engineering_global, org: test, grade: g0,\n"
        "   vertical: engineering, scope: global}\n"
    )
    gid_map = tmp_path / "gid_map.yml"
    gid_map.write_text("groups:\n  arc_g0_engineering_global: 1003\n")
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        _TWO_ORG_CFG.format(
            root=root, plan=plan, gid_map=gid_map, db=tmp_path / "state.db"
        )
    )
    return cfg


def test_ingest_without_org_exits_one_when_several_are_configured(tmp_path: Path) -> None:
    """§11 again: the org decides what is read and written, so it is declared
    or the tool stops. Picking the first would be the plausible default."""
    cfg = _two_org_cfg_file(tmp_path)
    result = runner.invoke(app, ["ingest", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "--org is required" in result.stdout + str(result.stderr or "")


def test_unknown_org_exits_one_and_names_the_configured_ones(tmp_path: Path) -> None:
    cfg = _two_org_cfg_file(tmp_path)
    result = runner.invoke(app, ["stats", "--config", str(cfg), "--org", "nope"])
    assert result.exit_code == 1
    assert "arc, job" in result.stdout + str(result.stderr or "")


def test_one_org_configured_needs_no_org_flag(tmp_path: Path) -> None:
    """The single-org deployment stays as simple as it was."""
    cfg = _cfg_file(tmp_path)
    result = runner.invoke(app, ["stats", "--config", str(cfg)])
    assert result.exit_code == 0
