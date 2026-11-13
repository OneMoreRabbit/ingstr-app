"""Classifying by a grant on a folder, and taking access away again.

Folders shared by an added ACL keep their own owner, group and mode on
purpose — rewriting ownership under a one-way copy is what broke the Zambia
sync — so their files carry no RBAC group and were never indexed. The plan's
`acl_grants` says who may read them.

Two halves, and the second is the one that matters: a grant can be WITHDRAWN,
and withdrawing it changes nothing on disk. Taking access away must take it
away in search.
"""

from pathlib import Path

import pytest

from ingstr.exceptions import AmbiguousGrant, PlanError, UnclassifiableFile
from ingstr.plan import load_plan

_PLAN = """\
required_groups:
- arc_g0_engineering_global
- arc_gis_share_global
- arc_other_share_global
groups:
- {name: arc_g0_engineering_global, org: arc, grade: g0,
   vertical: engineering, scope: global}
- {name: arc_gis_share_global, org: arc, grade: gis,
   vertical: share, scope: global}
- {name: arc_other_share_global, org: arc, grade: other,
   vertical: share, scope: global}
acl_grants:
- {path: 'arc/drive/zambia_data/', group: arc_gis_share_global, access: read,
   default_acl: true, description: '', source_file: orgs/arc.yml}
- {path: 'arc/drive/zambia_data/private/', group: arc_other_share_global,
   access: read, default_acl: true, description: '', source_file: orgs/arc.yml}
"""

_MAP = """\
groups:
  arc_g0_engineering_global: 1003
  arc_gis_share_global: 1500
  arc_other_share_global: 1501
"""


def _plan(tmp_yaml, plan_text: str = _PLAN):
    return load_plan(tmp_yaml("plan.yml", plan_text), tmp_yaml("map.yml", _MAP))


def test_grants_are_read_from_the_plan(tmp_yaml):
    plan = _plan(tmp_yaml)
    assert plan.grants == {
        "arc/drive/zambia_data/": frozenset({"arc_gis_share_global"}),
        "arc/drive/zambia_data/private/": frozenset({"arc_other_share_global"}),
    }


def test_the_deepest_granted_folder_wins(tmp_yaml):
    """The contract permits nested entries and says nothing about which
    applies, so this is ingstr's rule — and it is the only one that makes a
    narrower share mean anything."""
    plan = _plan(tmp_yaml)
    outer = plan.grant_for("arc/drive/zambia_data/report.pdf")
    inner = plan.grant_for("arc/drive/zambia_data/private/secret.pdf")
    assert outer is not None and outer[1] == frozenset({"arc_gis_share_global"})
    assert inner is not None and inner[1] == frozenset({"arc_other_share_global"})


def test_a_file_outside_every_grant_matches_nothing(tmp_yaml):
    plan = _plan(tmp_yaml)
    assert plan.grant_for("arc/drive/elsewhere/x.pdf") is None


def test_one_path_with_two_groups_is_kept_as_two(tmp_yaml):
    """Validation rejects only a duplicate of the same path AND tuple, so one
    folder may legitimately carry two grants. ingstr must not choose."""
    doubled = _PLAN + (
        "- {path: 'arc/drive/zambia_data/', group: arc_other_share_global,\n"
        "   access: read, default_acl: true, description: '',\n"
        "   source_file: orgs/arc.yml}\n"
    )
    plan = _plan(tmp_yaml, doubled)
    assert plan.grants["arc/drive/zambia_data/"] == frozenset(
        {"arc_gis_share_global", "arc_other_share_global"}
    )


def test_a_grant_path_in_the_wrong_form_is_refused(tmp_yaml):
    """A path with a leading slash, or without the trailing one, looks right
    and matches nothing — a silent empty result."""
    bad = _PLAN.replace("'arc/drive/zambia_data/'", "'/arc/drive/zambia_data/'", 1)
    with pytest.raises(PlanError, match="must not begin with"):
        _plan(tmp_yaml, bad)


def test_a_grant_to_a_group_outside_required_groups_is_refused(tmp_yaml):
    """The contract guarantees every grant's group is in required_groups. A
    plan that breaks it would have ingstr stamp a group no reader resolves."""
    bad = _PLAN.replace("group: arc_gis_share_global", "group: arc_not_a_group", 1)
    with pytest.raises(PlanError, match="not in required_groups"):
        _plan(tmp_yaml, bad)


def test_a_plan_with_no_grants_is_normal(tmp_yaml):
    without = _PLAN.split("acl_grants:")[0]
    plan = _plan(tmp_yaml, without)
    assert plan.grants == {}


# ── classify ───────────────────────────────────────────────────────────────


class _Stat:
    def __init__(self, st_gid: int) -> None:
        self.st_gid = st_gid


def test_a_granted_file_is_classified_by_its_grant(tmp_yaml):
    from ingstr.classify import classify

    plan = _plan(tmp_yaml)
    group = classify(
        Path("/mnt/data/zambia_data/report.pdf"),
        plan,
        "arc",
        stat_info=_Stat(st_gid=65534),  # nobody: not an RBAC group
        file_plan_path="arc/drive/zambia_data/report.pdf",
    )
    assert group == "arc_gis_share_global"


def test_the_file_s_own_group_wins_over_a_grant_above_it(tmp_yaml):
    """A classified file is never re-classified by a grant on a parent folder.
    Classification is the narrower, more deliberate statement, and a grant
    must not quietly widen the audience for a file classified on purpose."""
    from ingstr.classify import classify

    plan = _plan(tmp_yaml)
    group = classify(
        Path("/mnt/data/zambia_data/report.pdf"),
        plan,
        "arc",
        stat_info=_Stat(st_gid=1003),
        file_plan_path="arc/drive/zambia_data/report.pdf",
    )
    assert group == "arc_g0_engineering_global"


def test_a_file_under_two_grants_is_refused_not_chosen(tmp_yaml):
    from ingstr.classify import classify

    doubled = _PLAN + (
        "- {path: 'arc/drive/zambia_data/', group: arc_other_share_global,\n"
        "   access: read, default_acl: true, description: '',\n"
        "   source_file: orgs/arc.yml}\n"
    )
    plan = _plan(tmp_yaml, doubled)
    with pytest.raises(AmbiguousGrant) as exc:
        classify(
            Path("/mnt/data/zambia_data/report.pdf"),
            plan,
            "arc",
            stat_info=_Stat(st_gid=65534),
            file_plan_path="arc/drive/zambia_data/report.pdf",
        )
    assert exc.value.groups == frozenset(
        {"arc_gis_share_global", "arc_other_share_global"}
    )


def test_a_file_under_no_grant_is_still_refused(tmp_yaml):
    """Nothing becomes readable that the plan does not grant."""
    from ingstr.classify import classify

    plan = _plan(tmp_yaml)
    with pytest.raises(UnclassifiableFile):
        classify(
            Path("/mnt/data/elsewhere/x.pdf"),
            plan,
            "arc",
            stat_info=_Stat(st_gid=65534),
            file_plan_path="arc/drive/elsewhere/x.pdf",
        )


def test_without_plan_path_a_granted_file_is_refused_not_guessed(tmp_yaml):
    """The run is stopped before the walk when this is missing. Reaching here
    means that check was bypassed, and guessing is the one thing not to do."""
    from ingstr.classify import classify

    plan = _plan(tmp_yaml)
    with pytest.raises(UnclassifiableFile):
        classify(
            Path("/mnt/data/zambia_data/report.pdf"),
            plan,
            "arc",
            stat_info=_Stat(st_gid=65534),
            file_plan_path=None,
        )
