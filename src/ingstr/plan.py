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
    plan_source_path: Path
    map_source_path: Path


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
