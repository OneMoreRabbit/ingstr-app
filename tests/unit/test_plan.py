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
