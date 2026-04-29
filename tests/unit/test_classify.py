from pathlib import Path
from unittest.mock import patch

import pytest

from ingstr.classify import classify
from ingstr.exceptions import UnclassifiableFile
from ingstr.plan import ResolvedPlan


def _plan() -> ResolvedPlan:
    return ResolvedPlan(
        gid_to_group={1003: "arc_g0_engineering_global", 1004: "arc_g18_any_global"},
        required_groups=frozenset({"arc_g0_engineering_global", "arc_g18_any_global"}),
        plan_source_path=Path("/dev/null"),
        map_source_path=Path("/dev/null"),
    )


class _StatResult:
    def __init__(self, st_gid: int) -> None:
        self.st_gid = st_gid


def test_known_gid_resolves_to_group_name():
    plan = _plan()
    p = Path("/some/file.pdf")
    with patch.object(Path, "stat", return_value=_StatResult(st_gid=1003)):
        assert classify(p, plan) == "arc_g0_engineering_global"


def test_unknown_gid_raises_unclassifiable():
    plan = _plan()
    p = Path("/some/file.pdf")
    with patch.object(Path, "stat", return_value=_StatResult(st_gid=9999)):
        with pytest.raises(UnclassifiableFile) as exc_info:
            classify(p, plan)
    assert exc_info.value.gid == 9999
    assert exc_info.value.path == p


def test_unclassifiable_message_mentions_path_and_gid():
    plan = _plan()
    p = Path("/some/file.pdf")
    with patch.object(Path, "stat", return_value=_StatResult(st_gid=42)):
        with pytest.raises(UnclassifiableFile, match="gid 42"):
            classify(p, plan)
