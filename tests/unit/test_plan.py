import pytest

from ingstr.exceptions import PlanError
from ingstr.plan import load_plan

_VALID_PLAN = """\
required_groups:
- arc_g0_engineering_global
- arc_g18_any_global
"""

_VALID_MAP = """\
meta:
  generated_at: "2026-04-29T09:43:28Z"
  hostname: "beaver"
groups:
  arc_g0_engineering_global: 1003
  arc_g18_any_global: 1004
"""


def test_loads_and_inverts_map(tmp_yaml):
    plan = load_plan(
        tmp_yaml("compiled_plan.yml", _VALID_PLAN),
        tmp_yaml("group_gid_map.yml", _VALID_MAP),
    )
    assert plan.gid_to_group == {1003: "arc_g0_engineering_global", 1004: "arc_g18_any_global"}
    assert "arc_g0_engineering_global" in plan.required_groups


def test_missing_compiled_plan_raises(tmp_yaml, tmp_path):
    with pytest.raises(PlanError, match="missing"):
        load_plan(tmp_path / "absent.yml", tmp_yaml("group_gid_map.yml", _VALID_MAP))


def test_missing_gid_map_raises(tmp_yaml, tmp_path):
    with pytest.raises(PlanError, match="missing"):
        load_plan(tmp_yaml("compiled_plan.yml", _VALID_PLAN), tmp_path / "absent.yml")


def test_group_not_in_required_groups_raises(tmp_yaml):
    bad_map = _VALID_MAP + "  rogue_group: 1099\n"
    with pytest.raises(PlanError, match="absent from compiled_plan.yml"):
        load_plan(
            tmp_yaml("compiled_plan.yml", _VALID_PLAN),
            tmp_yaml("group_gid_map.yml", bad_map),
        )


def test_required_group_missing_from_map_raises(tmp_yaml):
    """The other direction of the same guarantee.

    `group-gid-map-v0_1` guarantee 1 is "key set == required_groups" — no extras
    AND no omissions. Only the extras half was checked until 2026-09-13, so a map
    missing a required group loaded cleanly and surfaced later as one
    UnclassifiableFile per affected file, instead of one upstream error naming
    the cause.
    """
    short_map = """\
groups:
  arc_g0_engineering_global: 1003
  arc_g18_any_global: 1004
"""
    plan_with_extra = _VALID_PLAN + "- arc_g9_finance_global\n"
    with pytest.raises(PlanError, match="missing groups that compiled_plan.yml"):
        load_plan(
            tmp_yaml("compiled_plan.yml", plan_with_extra),
            tmp_yaml("group_gid_map.yml", short_map),
        )


def test_renumbering_is_not_detected_within_a_run(tmp_yaml):
    """Documents a known blind spot rather than asserting desired behaviour.

    A host rebuild can reissue different GIDs for the same group names. The key
    set is unchanged, so both directions of the cross-check pass and the plan
    loads happily with silently different numbers — the same "adjacent
    invariant" shape ADR-0010 §6 records for the export sentinel. Catching it
    needs continuity across runs (§6 layer 2), which this function cannot see.

    This test exists so the gap is visible in the suite; change it when
    continuity checking lands, do not delete it.
    """
    renumbered = """\
groups:
  arc_g0_engineering_global: 5003
  arc_g18_any_global: 5004
"""
    plan = load_plan(
        tmp_yaml("compiled_plan.yml", _VALID_PLAN),
        tmp_yaml("group_gid_map.yml", renumbered),
    )
    assert plan.gid_to_group == {5003: "arc_g0_engineering_global", 5004: "arc_g18_any_global"}


def test_duplicate_gid_raises(tmp_yaml):
    dup_map = """\
groups:
  arc_g0_engineering_global: 1003
  arc_g18_any_global: 1003
"""
    with pytest.raises(PlanError, match="duplicate gid"):
        load_plan(
            tmp_yaml("compiled_plan.yml", _VALID_PLAN),
            tmp_yaml("group_gid_map.yml", dup_map),
        )


def test_non_int_gid_raises(tmp_yaml):
    bad_map = """\
groups:
  arc_g0_engineering_global: "not_a_number"
"""
    with pytest.raises(PlanError, match="must be an int"):
        load_plan(
            tmp_yaml("compiled_plan.yml", _VALID_PLAN),
            tmp_yaml("group_gid_map.yml", bad_map),
        )


def test_required_groups_must_be_list(tmp_yaml):
    bad_plan = "required_groups: not_a_list\n"
    with pytest.raises(PlanError, match="must be a list"):
        load_plan(
            tmp_yaml("compiled_plan.yml", bad_plan),
            tmp_yaml("group_gid_map.yml", _VALID_MAP),
        )


# ── v0.2.0: groups[] and per-org gids (compiled-rbac-plan 0.11) ─────────────

_PLAN_WITH_GROUPS = """\
required_groups:
- arc_g0_engineering_global
- job_g1_personal_global
groups:
- {name: arc_g0_engineering_global, org: arc, grade: g0,
   vertical: engineering, scope: global}
- {name: job_g1_personal_global, org: job, grade: g1,
   vertical: personal, scope: global}
"""

_MAP_TWO = """\
groups:
  arc_g0_engineering_global: 1003
  job_g1_personal_global: 1200
"""


def test_groups_section_gives_each_group_its_org(tmp_yaml):
    plan = load_plan(
        tmp_yaml("plan.yml", _PLAN_WITH_GROUPS), tmp_yaml("map.yml", _MAP_TWO)
    )
    assert plan.org_of_group("arc_g0_engineering_global") == "arc"
    assert plan.org_of_group("job_g1_personal_global") == "job"


def test_gids_for_org_narrows_to_that_org(tmp_yaml):
    """The whole point: a run holds only its own org's group ids, so the mount
    decides what is visible and this decides what is readable."""
    plan = load_plan(
        tmp_yaml("plan.yml", _PLAN_WITH_GROUPS), tmp_yaml("map.yml", _MAP_TWO)
    )
    assert plan.gids_for_org("arc") == frozenset({1003})
    assert plan.gids_for_org("job") == frozenset({1200})
    assert plan.gids_for_org("nobody") == frozenset()


def test_plan_without_groups_section_is_not_an_error(tmp_yaml):
    """`groups[]` is additive in 0.11; a plan below it is legal. No org is
    known, so nothing is narrowed — reported by `ingstr health`, not guessed."""
    plan = load_plan(
        tmp_yaml("plan.yml", "required_groups:\n- arc_g0_engineering_global\n"),
        tmp_yaml("map.yml", "groups:\n  arc_g0_engineering_global: 1003\n"),
    )
    assert plan.group_org == {}
    assert plan.org_of_group("arc_g0_engineering_global") is None


def test_groups_disagreeing_with_required_groups_is_refused(tmp_yaml):
    """0.11 guarantees the two are equal element for element. If they are not,
    a reader would classify differently depending on which it believed."""
    bad = "\n".join(
        line for line in _PLAN_WITH_GROUPS.splitlines()
        if "job_g1_personal_global, org: job" not in line
        and "vertical: personal" not in line
    ) + "\n"
    with pytest.raises(PlanError, match="disagree"):
        load_plan(tmp_yaml("plan.yml", bad), tmp_yaml("map.yml", _MAP_TWO))


def test_groups_row_missing_org_is_refused(tmp_yaml):
    bad = _PLAN_WITH_GROUPS.replace(", org: job", "")
    with pytest.raises(PlanError, match="'name' and 'org'"):
        load_plan(tmp_yaml("plan.yml", bad), tmp_yaml("map.yml", _MAP_TWO))
