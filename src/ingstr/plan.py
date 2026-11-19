from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .exceptions import PlanError


@dataclass(frozen=True)
class AgentFolder:
    """One agent folder the plan declares, as beaver sees it.

    `compiled-rbac-plan` 0.12, section `agent_folders[]`. The group comes from
    the declaration and never from the files: a seat's conversations belong to
    its home user, so their group says nothing about who may read them.
    """

    name: str
    kind: str
    host: str
    org: str
    folder: str
    #: `<host>/<org>/<name>/<folder>/`, relative to the `agent_hosts` root,
    #: in the plan's path form (no leading slash, one trailing slash).
    path: str
    group: str
    indexed: bool


#: The folders the design names (`agent-folders-design-v0_1` §2). `configs` is
#: never indexed; it is listed so a row naming it is a known row with
#: `indexed: false` rather than an unknown folder.
_AGENT_FOLDERS = frozenset({"sessions", "memory", "scratch", "configs"})


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
    #: Every row of `agent_folders[]`, or an empty tuple when the plan carries
    #: no section (0.11 and earlier). Whether that absence is an error depends
    #: on whether this org configured agent folders, which the plan cannot
    #: know — so the refusal lives in the caller, not here.
    agent_folders: tuple[AgentFolder, ...] = ()
    #: True when the plan carried an `agent_folders` section at all, even
    #: empty. An empty section means "no agent declares folders"; a missing
    #: one means "this plan is older than 0.12", and they call for different
    #: answers.
    declares_agent_folders: bool = False
    plan_source_path: Path = Path("/dev/null")
    map_source_path: Path = Path("/dev/null")

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

    def indexed_folders_for_org(self, org: str) -> tuple[AgentFolder, ...]:
        """The agent folders this org indexes, in the plan's order.

        Only rows marked `indexed`. `configs` is never indexed and the contract
        says so, but this reads the flag rather than the name: a deployment
        should be able to see that a folder is not indexed, not have to know
        it.
        """
        return tuple(f for f in self.agent_folders if f.org == org and f.indexed)


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
    agent_folders, declares_agent_folders = _load_agent_folders(
        compiled_plan_path, required_groups
    )
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
        agent_folders=agent_folders,
        declares_agent_folders=declares_agent_folders,
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


def _load_agent_folders(
    path: Path, required_groups: set[str]
) -> tuple[tuple[AgentFolder, ...], bool]:
    """Read `agent_folders[]`: the agents' own folders, as beaver sees them.

    `compiled-rbac-plan` 0.12. Each row declares one folder of one agent or
    seat, and the group that classifies everything in it. The declaration is
    the whole point: a seat's files belong to its home user (uid 1000), so
    their group says nothing about who may read them
    ([[agent-folders-design-v0_1]] §5).

    Returns the rows and whether the section was present at all. An EMPTY
    section means no agent has declared folders; a MISSING one means the plan
    predates 0.12. Only the caller knows whether this org configured agent
    folders, so only the caller can say which of those is an error — this
    function reports the difference rather than choosing.
    """
    raw = _read_yaml(path)
    rows = raw.get("agent_folders")
    if rows is None:
        return (), False
    if not isinstance(rows, list):
        raise PlanError(
            f"{path}: 'agent_folders' must be a list of rows (compiled-rbac-plan 0.12)"
        )
    out: list[AgentFolder] = []
    for row in rows:
        if not isinstance(row, dict):
            raise PlanError(f"{path}: each 'agent_folders' row must be a mapping, got {row!r}")
        missing = [
            k for k in ("name", "kind", "host", "org", "folder", "path", "group")
            if not isinstance(row.get(k), str)
        ]
        if missing:
            raise PlanError(
                f"{path}: an 'agent_folders' row is missing {missing} "
                f"(compiled-rbac-plan 0.12): {row!r}"
            )
        indexed = row.get("indexed")
        if not isinstance(indexed, bool):
            raise PlanError(
                f"{path}: 'agent_folders' row for {row['name']}/{row['folder']} needs a "
                "boolean 'indexed'. A deployment should be able to READ that a folder is "
                "not indexed, not have to know it from the folder's name"
            )
        folder = row["folder"]
        if folder not in _AGENT_FOLDERS:
            raise PlanError(
                f"{path}: 'agent_folders' row names folder {folder!r}, which is not one of "
                f"{sorted(_AGENT_FOLDERS)} (agent-folders-design-v0_1 §2)"
            )
        if folder == "configs" and indexed:
            raise PlanError(
                f"{path}: 'agent_folders' marks {row['name']}'s 'configs' as indexed. "
                "configs is never indexed (design D7): it holds settings and is a cleaned "
                "copy, and indexing it would put an agent's configuration into search"
            )
        path_value = row["path"]
        if path_value.startswith("/") or not path_value.endswith("/"):
            raise PlanError(
                f"{path}: 'agent_folders' path {path_value!r} must be written as the plan "
                "writes paths: no leading slash, exactly one trailing slash"
            )
        if row["group"] not in required_groups:
            raise PlanError(
                f"{path}: 'agent_folders' row for {row['name']}/{folder} names group "
                f"{row['group']!r}, which is not in required_groups. The contract "
                "guarantees it is, so this plan is inconsistent with itself"
            )
        out.append(
            AgentFolder(
                name=row["name"],
                kind=row["kind"],
                host=row["host"],
                org=row["org"],
                folder=folder,
                path=path_value,
                group=row["group"],
                indexed=indexed,
            )
        )
    return tuple(out), True
