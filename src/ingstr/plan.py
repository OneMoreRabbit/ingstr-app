from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .exceptions import PlanError


@dataclass(frozen=True)
class ResolvedPlan:
    """Authoritative classification state for one Ingstr run.

    `gid_to_group` is the inverted `group_gid_map.yml` and is the source of truth
    for per-file classification. `required_groups` is the canonical name set from
    `compiled_plan.yml` and is used only to cross-validate the GID map.
    """

    gid_to_group: dict[int, str]
    required_groups: frozenset[str]
    group_org: dict[str, str]
    #: Every `acl_grants` path and the groups granted on it, the path exactly
    #: as the plan writes it (no leading slash, one trailing slash, relative
    #: to `meta.path_roots.acl`). A path may legally carry several grants with
    #: different groups, which is why the value is a set and not a name.
    grants: dict[str, frozenset[str]]
    plan_source_path: Path
    map_source_path: Path

    def grant_for(self, plan_path: str) -> tuple[str, frozenset[str]] | None:
        """The grant governing `plan_path`, or None.

        `plan_path` is the file's path below the plan's `acl` root, in the
        plan's own form. The DEEPEST granted directory containing it wins:
        the contract permits nested entries and says nothing about which
        applies, so this is ingstr's rule and it is the only one that makes a
        narrower share mean anything.

        Returns the winning path with the groups granted on it — more than one
        when the same path carries several grants, which the caller refuses
        rather than choosing between.
        """
        best: tuple[str, frozenset[str]] | None = None
        for path, groups in self.grants.items():
            if plan_path.startswith(path) and (best is None or len(path) > len(best[0])):
                best = (path, groups)
        return best

    def gids_for_org(self, org: str) -> frozenset[int]:
        """The GIDs of the groups belonging to `org`.

        A run reads one org's folder, so it needs only that org's groups
        (one-search-service design, accepted 2026-11-13). Narrowing the
        supplementary groups narrows what the process can read at all, which
        is the point: the mount decides what is visible, this decides what is
        readable, and the two then agree.
        """
        return frozenset(
            gid for gid, name in self.gid_to_group.items()
            if self.group_org.get(name) == org
        )

    def org_of_group(self, group: str) -> str | None:
        return self.group_org.get(group)


def load_plan(compiled_plan_path: Path, group_gid_map_path: Path) -> ResolvedPlan:
    """Load both upstream YAMLs, invert the GID map, and cross-validate.

    Raises PlanError if either file is missing/malformed, or if the two files
    disagree about the group set in *either* direction (a stale or inconsistent
    map is fail-fast — exit code 1).

    The check is deliberately symmetric. `group-gid-map-v0_1` guarantee 1 states
    the key set is exactly `required_groups` — "no extras, no omissions" — and the
    contract's regeneration rule says ingstr "cross-validates the map against
    required_groups at startup and exits 1 on mismatch". Until 2026-09-13 this
    function only checked one of those directions, so a map *missing* a required
    group loaded cleanly and every file owned by that group then failed
    individually as UnclassifiableFile — fail-closed, but reported as many
    per-file errors rather than the one upstream fault that caused them.

    Note what this still cannot catch: a **GID renumbering** with an unchanged key
    set (a host rebuild reissuing different numbers for the same names) satisfies
    both directions and is invisible here. That is the contract's own open
    question, and it is the same failure shape ADR-0010 §6 records for the export
    sentinel — a check validating an invariant *adjacent* to the one that matters.
    Detecting it needs continuity across runs, not a within-run comparison; see
    ADR-0010 §6 layer 2 for the shape the fix takes.
    """
    required_groups = _load_required_groups(compiled_plan_path)
    group_org = _load_group_orgs(compiled_plan_path, required_groups)
    grants = _load_acl_grants(compiled_plan_path, required_groups)
    name_to_gid = _load_group_gid_map(group_gid_map_path)

    unknown = sorted(set(name_to_gid) - required_groups)
    if unknown:
        raise PlanError(
            f"group_gid_map.yml lists groups absent from compiled_plan.yml's "
            f"required_groups (stale map?): {unknown}"
        )

    missing = sorted(required_groups - set(name_to_gid))
    if missing:
        raise PlanError(
            f"group_gid_map.yml is missing groups that compiled_plan.yml's "
            f"required_groups declares (stale map, or export ran before the "
            f"groups existed?): {missing}"
        )

    gid_to_group: dict[int, str] = {}
    for name, gid in name_to_gid.items():
        if gid in gid_to_group:
            raise PlanError(
                f"duplicate gid {gid} in group_gid_map.yml: "
                f"both '{gid_to_group[gid]}' and '{name}'"
            )
        gid_to_group[gid] = name

    return ResolvedPlan(
        gid_to_group=gid_to_group,
        required_groups=frozenset(required_groups),
        group_org=group_org,
        grants=grants,
        plan_source_path=compiled_plan_path,
        map_source_path=group_gid_map_path,
    )


def _load_required_groups(path: Path) -> set[str]:
    raw = _read_yaml(path)
    groups = raw.get("required_groups")
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        raise PlanError(
            f"{path}: 'required_groups' must be a list of strings"
        )
    return set(groups)


def _load_group_orgs(path: Path, required_groups: set[str]) -> dict[str, str]:
    """Read each group's org from the plan's `groups[]` section.

    `compiled-rbac-plan` 0.11 states a group's org outright so a consumer does
    not parse it out of the group name (constitution §10).

    **ingstr v0.2.0 requires the section.** It is additive in the contract, so
    an older plan is legal there — but a run narrows its group ids by org, and
    without the section it cannot. The alternative considered and rejected was
    to fall back to every group id in the estate-wide map and report it: that
    is the behaviour v0.2.0 exists to end, it is what the estate's current
    plan would have produced, and a fallback nobody notices is how it would
    have stayed. Arch ruled it an error (hub #13871); the estate rolls
    rbac-compiler v0.7.3 first.

    Guarantee checked: `groups[].name` equals `required_groups`, element for
    element. A plan that carries the section but disagrees with its own
    `required_groups` is refused, because the two would then classify
    differently depending on which a reader believed.
    """
    raw = _read_yaml(path)
    rows = raw.get("groups")
    if rows is None:
        raise PlanError(
            f"{path}: no 'groups' section. ingstr v0.2.0 narrows a run's group "
            "ids to its own org and cannot do that without it; it arrived in "
            "compiled-rbac-plan 0.11 (rbac-compiler v0.7.3). Upgrade the "
            "compiler, or run ingstr v0.1.1."
        )
    if not isinstance(rows, list):
        raise PlanError(f"{path}: 'groups' must be a list of rows (compiled-rbac-plan 0.11)")
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PlanError(f"{path}: each 'groups' row must be a mapping, got {row!r}")
        name, org = row.get("name"), row.get("org")
        if not isinstance(name, str) or not isinstance(org, str):
            raise PlanError(
                f"{path}: each 'groups' row needs string 'name' and 'org', got {row!r}"
            )
        out[name] = org
    named = set(out)
    if named != required_groups:
        missing = sorted(required_groups - named)
        extra = sorted(named - required_groups)
        raise PlanError(
            f"{path}: 'groups' and 'required_groups' disagree "
            f"(contract 0.11 guarantees they are equal element for element); "
            f"missing from groups: {missing}; not in required_groups: {extra}"
        )
    return out


def _load_acl_grants(path: Path, required_groups: set[str]) -> dict[str, frozenset[str]]:
    """Read `acl_grants[]`: folders shared by an added ACL rather than by
    classification.

    Such a folder keeps its own owner, group and mode on purpose — that is the
    whole point of the mechanism, and rewriting ownership under a one-way copy
    is what broke the Zambia sync. So its files carry no RBAC group and would
    never be indexed without this section.

    Absent or empty is normal and not an error: a plan with no grants simply
    grants nothing. Only the fields ingstr needs are read; the rest of the
    entry belongs to whoever applies the ACL.
    """
    raw = _read_yaml(path)
    rows = raw.get("acl_grants")
    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise PlanError(f"{path}: 'acl_grants' must be a list of entries")
    out: dict[str, set[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PlanError(f"{path}: each 'acl_grants' entry must be a mapping, got {row!r}")
        grant_path, group = row.get("path"), row.get("group")
        if not isinstance(grant_path, str) or not isinstance(group, str):
            raise PlanError(
                f"{path}: each 'acl_grants' entry needs string 'path' and 'group', got {row!r}"
            )
        if grant_path.startswith("/") or not grant_path.endswith("/"):
            # The plan's path convention, stated in the contract: no leading
            # slash, exactly one trailing slash. Checked because a path that
            # breaks it would silently match nothing, or match too much.
            raise PlanError(
                f"{path}: 'acl_grants' path {grant_path!r} must not begin with "
                "'/' and must end with exactly one '/'"
            )
        if group not in required_groups:
            # The contract guarantees this. A plan that breaks it would have
            # ingstr stamp a group no consumer can resolve.
            raise PlanError(
                f"{path}: 'acl_grants' grants {group!r} on {grant_path!r}, "
                "which is not in required_groups"
            )
        out.setdefault(grant_path, set()).add(group)
    return {p: frozenset(g) for p, g in out.items()}


def _load_group_gid_map(path: Path) -> dict[str, int]:
    raw = _read_yaml(path)
    groups = raw.get("groups")
    if not isinstance(groups, dict):
        raise PlanError(f"{path}: 'groups' must be a mapping of name → gid")
    out: dict[str, int] = {}
    for name, gid in groups.items():
        if not isinstance(name, str):
            raise PlanError(f"{path}: group name must be a string, got {name!r}")
        if not isinstance(gid, int) or isinstance(gid, bool):
            raise PlanError(f"{path}: gid for {name!r} must be an int, got {gid!r}")
        out[name] = gid
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PlanError(f"required upstream file missing: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise PlanError(f"{path}: not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise PlanError(f"{path}: root must be a mapping")
    return raw
