"""ADR-0010 §7 enumeration, and §6's "any errno is a skip, never an empty".

Fault injection via chmod — the errno reachable without a real mount. It stands
in for ESTALE/EIO/ETIMEDOUT, which no seat can produce: the code path under test
is the blanket OSError arm, deliberately not an enumeration of errnos so an
unanticipated one cannot fall through to "this agent has no files".
"""

import os
from pathlib import Path

import pytest

from ingstr.agent_sources import iter_agent_scopes

SURFACES = ["memory", "sessions", "scratch"]


def _tree(root: Path, spec: dict[str, dict[str, list[str]]]) -> Path:
    """spec: {org: {agent: [surfaces]}}"""
    for org, agents in spec.items():
        for agent, surfaces in agents.items():
            for surface in surfaces:
                (root / org / agent / surface).mkdir(parents=True)
    return root


def test_enumerates_org_agent_surface(tmp_path: Path) -> None:
    root = _tree(tmp_path / "mnt", {"arc": {"research_mz": ["memory", "scratch"]}})
    result = iter_agent_scopes(root, SURFACES)

    assert [s.stem for s in result.scopes] == [
        "arc/research_mz/memory",
        "arc/research_mz/scratch",
    ]
    assert result.skips == []


def test_stem_excludes_the_root_by_design(tmp_path: Path) -> None:
    """§7: tools share the stem, not the absolute path.

    rbac-compile prepends the host root and ingstr prepends the beaver mount
    root; the guarantee between them is stem equality. If the root ever leaks
    into the stem, that cross-tool agreement breaks silently.
    """
    root = _tree(tmp_path / "mnt" / "agent-hosts" / "otter", {"arc": {"a1": ["memory"]}})
    scope = iter_agent_scopes(root, SURFACES).scopes[0]

    assert scope.stem == "arc/a1/memory"
    assert scope.agent_stem == "arc/a1"
    assert str(root) not in scope.stem
    assert scope.path == root / "arc" / "a1" / "memory"


def test_absent_surface_is_not_a_skip(tmp_path: Path) -> None:
    """A new agent with no scratch/ yet is ordinary, not a fault.

    Reporting it would bury the real skips in noise, which is how a notice
    channel stops being read.
    """
    root = _tree(tmp_path / "mnt", {"arc": {"a1": ["memory"]}})
    result = iter_agent_scopes(root, SURFACES)

    assert [s.stem for s in result.scopes] == ["arc/a1/memory"]
    assert result.skips == []


def test_configs_is_never_enumerated(tmp_path: Path) -> None:
    """It is not in the export; if it ever appears, we still do not read it."""
    root = _tree(tmp_path / "mnt", {"arc": {"a1": ["memory", "configs"]}})
    result = iter_agent_scopes(root, SURFACES)

    assert all("configs" not in s.stem for s in result.scopes)


def test_multiple_orgs_and_agents_are_sorted(tmp_path: Path) -> None:
    """Deterministic order: a run's notes and logs should diff cleanly."""
    root = _tree(
        tmp_path / "mnt",
        {"zeta": {"b": ["memory"]}, "arc": {"b": ["memory"], "a": ["memory"]}},
    )
    stems = [s.stem for s in iter_agent_scopes(root, SURFACES).scopes]
    assert stems == ["arc/a/memory", "arc/b/memory", "zeta/b/memory"]


# ── §6: unreadable is never empty ───────────────────────────────────────────


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_unreadable_org_is_skipped_not_silently_empty(tmp_path: Path) -> None:
    """The case that would otherwise read as "every agent in this org deleted everything"."""
    root = _tree(tmp_path / "mnt", {"arc": {"a1": ["memory"]}, "cpf": {"b1": ["memory"]}})
    (root / "cpf").chmod(0o000)
    try:
        result = iter_agent_scopes(root, SURFACES)
    finally:
        (root / "cpf").chmod(0o755)

    assert [s.stem for s in result.scopes] == ["arc/a1/memory"]
    assert len(result.skips) == 1
    assert result.skips[0].stem == "cpf"
    assert "cannot list org cpf" in result.skips[0].reason
    assert result.skipped_orgs == ["cpf"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_unreadable_surface_is_attributed_to_its_agent(tmp_path: Path) -> None:
    """Attribution matters: a short file list with no reason is the silent failure."""
    root = _tree(tmp_path / "mnt", {"arc": {"a1": ["memory", "scratch"]}})
    (root / "arc" / "a1" / "scratch").chmod(0o000)
    try:
        result = iter_agent_scopes(root, SURFACES)
    finally:
        (root / "arc" / "a1" / "scratch").chmod(0o755)

    assert [s.stem for s in result.scopes] == ["arc/a1/memory"]
    assert [s.stem for s in result.skips] == ["arc/a1/scratch"]
    assert result.skipped_orgs == ["arc"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_one_unreadable_agent_does_not_abort_the_run(tmp_path: Path) -> None:
    """Skip that agent and continue with the orgs we can see (§6)."""
    root = _tree(
        tmp_path / "mnt",
        {"arc": {"good": ["memory"], "bad": ["memory"]}},
    )
    (root / "arc" / "bad" / "memory").chmod(0o000)
    try:
        result = iter_agent_scopes(root, SURFACES)
    finally:
        (root / "arc" / "bad" / "memory").chmod(0o755)

    assert [s.stem for s in result.scopes] == ["arc/good/memory"]
    assert len(result.skips) == 1


def test_unreadable_export_root_yields_no_scopes_and_one_skip(tmp_path: Path) -> None:
    """Absent root: zero scopes, and a skip explaining it — never a clean empty."""
    result = iter_agent_scopes(tmp_path / "never-mounted", SURFACES)

    assert result.scopes == []
    assert len(result.skips) == 1
    assert "cannot list export root" in result.skips[0].reason


def test_errno_is_named_in_the_reason(tmp_path: Path) -> None:
    """`ESTALE` vs `EACCES` is "the host went away" vs "fix the ACL".

    An operator reading the note needs to tell those apart without reproducing
    the failure themselves.
    """
    result = iter_agent_scopes(tmp_path / "never-mounted", SURFACES)
    reason = result.skips[0].reason
    assert "FileNotFoundError" in reason
    assert "(2)" in reason  # ENOENT, carried through rather than swallowed


def test_a_file_where_a_directory_belongs_is_not_enumerated(tmp_path: Path) -> None:
    """Junk in the tree must not become a scope."""
    root = tmp_path / "mnt"
    (root / "arc" / "a1").mkdir(parents=True)
    (root / "arc" / "a1" / "memory").write_text("not a directory")
    (root / "loose-file.txt").write_text("x")

    result = iter_agent_scopes(root, SURFACES)
    assert result.scopes == []
    assert result.skips == []
