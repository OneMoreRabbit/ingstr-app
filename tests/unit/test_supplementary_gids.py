"""The group ids the entrypoint gives a run, and when it must refuse to start.

The entrypoint drops privileges before ingstr runs, so these ids decide what
the process can read at all. The distinction this file exists to protect: a
command that needs no groups runs without them, and a command that needs them
but cannot work them out STOPS. Falling back to "no groups" would turn an
unreadable plan into a run that calls every file unclassifiable — a plausible
result produced by a fault.
"""

from pathlib import Path

from ingstr.supplementary_gids import gids_for_run, main

_CFG = """\
orgs:
  - org: arc
    source: {{root: {root}}}
    qdrant: {{url: 'http://q', api_key_env: K, collection: c}}
    state: {{db_path: {db}}}
  - org: job
    source: {{root: {root}}}
    qdrant: {{url: 'http://q', api_key_env: K, collection: c}}
    state: {{db_path: {db}}}
plan:
  compiled_plan_path: {plan}
  group_gid_map_path: {gid_map}
embedding:
  endpoint: http://ollama:11434
  model: nomic-embed-text
  vector_dim: 4
"""

_PLAN = """\
required_groups:
- arc_g0_engineering_global
- job_g1_personal_global
groups:
- {name: arc_g0_engineering_global, org: arc, grade: g0,
   vertical: engineering, scope: global}
- {name: job_g1_personal_global, org: job, grade: g1,
   vertical: personal, scope: global}
"""

_MAP = """\
groups:
  arc_g0_engineering_global: 1003
  job_g1_personal_global: 1200
"""


def _write(tmp_path: Path, plan_text: str = _PLAN) -> Path:
    root = tmp_path / "src"
    root.mkdir()
    plan = tmp_path / "plan.yml"
    plan.write_text(plan_text)
    gid_map = tmp_path / "map.yml"
    gid_map.write_text(_MAP)
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        _CFG.format(root=root, db=tmp_path / "state.db", plan=plan, gid_map=gid_map)
    )
    return cfg


def test_only_the_org_s_own_gids(tmp_path: Path) -> None:
    cfg = _write(tmp_path)
    assert gids_for_run(cfg, "arc") == [1003]
    assert gids_for_run(cfg, "job") == [1200]


def test_without_groups_section_every_gid_is_kept(tmp_path: Path) -> None:
    """A plan below contract 0.11 states no org, so narrowing is not possible.
    Keeping every gid preserves today's behaviour rather than silently reading
    nothing; `ingstr health` reports that it was not narrowed."""
    cfg = _write(
        tmp_path,
        "required_groups:\n- arc_g0_engineering_global\n- job_g1_personal_global\n",
    )
    assert gids_for_run(cfg, "arc") == [1003, 1200]


def test_main_prints_a_comma_list(tmp_path: Path, capsys) -> None:
    cfg = _write(tmp_path)
    assert main([str(cfg), "arc"]) == 0
    assert capsys.readouterr().out.strip() == "1003"


def test_main_refuses_when_the_plan_cannot_be_read(tmp_path: Path, capsys) -> None:
    """The case the entrypoint must not paper over."""
    cfg = _write(tmp_path)
    (tmp_path / "plan.yml").unlink()
    assert main([str(cfg), "arc"]) == 1
    assert "cannot determine supplementary group ids" in capsys.readouterr().err


def test_main_refuses_an_unknown_org(tmp_path: Path, capsys) -> None:
    cfg = _write(tmp_path)
    assert main([str(cfg), "nope"]) == 1
    assert "arc, job" in capsys.readouterr().err


def test_main_refuses_when_the_org_is_ambiguous(tmp_path: Path, capsys) -> None:
    """Two orgs configured and no --org: the entrypoint must stop rather than
    hand the run one org's groups and let it read another's folder."""
    cfg = _write(tmp_path)
    assert main([str(cfg), ""]) == 1
    assert "--org is required" in capsys.readouterr().err
