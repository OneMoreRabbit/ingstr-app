from dataclasses import dataclass
from pathlib import Path

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

    Raises PlanError if either file is missing/malformed, or if any group in
    group_gid_map.yml is absent from compiled_plan.yml's required_groups
    (a stale or inconsistent map is fail-fast — exit code 1).
    """
    required_groups = _load_required_groups(compiled_plan_path)
    name_to_gid = _load_group_gid_map(group_gid_map_path)

    unknown = sorted(set(name_to_gid) - required_groups)
    if unknown:
        raise PlanError(
            f"group_gid_map.yml lists groups absent from compiled_plan.yml's "
            f"required_groups (stale map?): {unknown}"
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


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise PlanError(f"required upstream file missing: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise PlanError(f"{path}: not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise PlanError(f"{path}: root must be a mapping")
    return raw
