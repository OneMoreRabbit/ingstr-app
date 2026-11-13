from pathlib import Path
from unittest.mock import patch

import pytest

from ingstr.classify import classify
from ingstr.exceptions import UnclassifiableFile, WrongOrgFile
from ingstr.plan import ResolvedPlan


def _plan(group_org: dict[str, str] | None = None) -> ResolvedPlan:
    return ResolvedPlan(
        gid_to_group={
            1003: "arc_g0_engineering_global",
            1004: "arc_g18_any_global",
            1200: "job_g1_personal_global",
        },
        required_groups=frozenset(
            {"arc_g0_engineering_global", "arc_g18_any_global", "job_g1_personal_global"}
        ),
        group_org=group_org if group_org is not None else {},
        plan_source_path=Path("/dev/null"),
        map_source_path=Path("/dev/null"),
    )


_ORGS = {
    "arc_g0_engineering_global": "arc",
    "arc_g18_any_global": "arc",
    "job_g1_personal_global": "job",
}


class _StatResult:
    def __init__(self, st_gid: int) -> None:
        self.st_gid = st_gid


def test_known_gid_resolves_to_group_name():
    plan = _plan()
    p = Path("/some/file.pdf")
    with patch.object(Path, "stat", return_value=_StatResult(st_gid=1003)):
        assert classify(p, plan, "arc") == "arc_g0_engineering_global"


def test_unknown_gid_raises_unclassifiable():
    plan = _plan()
    p = Path("/some/file.pdf")
    with (
        patch.object(Path, "stat", return_value=_StatResult(st_gid=9999)),
        pytest.raises(UnclassifiableFile) as exc_info,
    ):
        classify(p, plan, "arc")
    assert exc_info.value.gid == 9999
    assert exc_info.value.path == p


def test_unclassifiable_message_mentions_path_and_gid():
    plan = _plan()
    p = Path("/some/file.pdf")
    with (
        patch.object(Path, "stat", return_value=_StatResult(st_gid=42)),
        pytest.raises(UnclassifiableFile, match="gid 42"),
    ):
        classify(p, plan, "arc")


# ── v0.2.0: a run classifies only its own org ───────────────────────────────


def test_group_of_another_org_is_refused():
    """A file in arc's folder owned by a job group is not indexed under arc.

    It is a separate error from unclassifiable on purpose: the estate HAS
    classified this file, into an org this run is not reading, which is a fact
    someone should look at rather than a missing classification.
    """
    plan = _plan(_ORGS)
    p = Path("/mnt/data/stray.pdf")
    with (
        patch.object(Path, "stat", return_value=_StatResult(st_gid=1200)),
        pytest.raises(WrongOrgFile) as exc_info,
    ):
        classify(p, plan, "arc")
    assert exc_info.value.group == "job_g1_personal_global"
    assert exc_info.value.group_org == "job"
    assert exc_info.value.run_org == "arc"


def test_own_org_group_still_classifies_when_orgs_are_known():
    plan = _plan(_ORGS)
    p = Path("/mnt/data/ok.pdf")
    with patch.object(Path, "stat", return_value=_StatResult(st_gid=1004)):
        assert classify(p, plan, "arc") == "arc_g18_any_global"


def test_without_groups_section_the_org_check_cannot_fire():
    """A plan below contract 0.11 states no group's org, so nothing is known
    to be wrong. The check is absent, not silently passing: `ingstr health`
    reports that the group ids were not narrowed."""
    plan = _plan({})
    p = Path("/mnt/data/stray.pdf")
    with patch.object(Path, "stat", return_value=_StatResult(st_gid=1200)):
        assert classify(p, plan, "arc") == "job_g1_personal_global"
