import os
from pathlib import Path

from .exceptions import AmbiguousGrant, UnclassifiableFile, WrongOrgFile
from .plan import ResolvedPlan


def classify(
    path: Path,
    plan: ResolvedPlan,
    org: str,
    *,
    stat_info: os.stat_result | None = None,
    file_plan_path: str | None = None,
) -> str:
    """Resolve a file to its canonical classification group.

    Two ways, in this order, and the order is the design:

    1. **The file's own group**, through the plan's GID map. A file the estate
       has classified says so itself, and that is a property of the file.
    2. **A grant on a folder above it**, from the plan's `acl_grants`, matched
       on `file_plan_path` — this file's path as the plan would write it. Folders
       shared by an added ACL keep their own owner, group and mode on purpose
       — rewriting ownership under a one-way copy is what broke the Zambia
       sync — so their files carry no RBAC group and would never be indexed
       without this.

    Trying the file's own group first means a classified file is never
    re-classified by a grant above it: classification is the narrower, more
    deliberate statement, and a grant on a parent folder must not quietly
    widen the audience for a file that was classified on purpose.

    Raises:
        UnclassifiableFile: neither applies. Fail-closed; callers skip the
            file and never substitute a default group. **Nothing becomes
            readable that the plan does not grant.**
        WrongOrgFile: it resolves, to another org's group.
        AmbiguousGrant: the governing grant carries more than one group.
    """
    gid = (stat_info if stat_info is not None else path.stat()).st_gid
    group = plan.gid_to_group.get(gid)
    if group is None:
        group = _group_from_grant(path, gid, plan, file_plan_path)
    group_org = plan.org_of_group(group)
    if group_org is not None and group_org != org:
        raise WrongOrgFile(path, gid, group, group_org, org)
    return group


def _group_from_grant(
    path: Path, gid: int, plan: ResolvedPlan, file_plan_path: str | None
) -> str:
    """The group granted on the deepest granted folder above the file.

    `file_plan_path` is this file's path as the PLAN would write it — the
    org's `source.plan_path` joined to the file's path below `source.root`.
    The join is the caller's because only the configuration knows the first
    half and only the walk knows the second.
    """
    if not plan.grants:
        raise UnclassifiableFile(path, gid)
    if file_plan_path is None:
        # The plan has grants and nothing says where this mount sits beneath
        # the plan's root, so no file can be matched against them. Refusing to
        # start is handled before the walk; reaching here means that check was
        # bypassed, and guessing is the one thing not to do.
        raise UnclassifiableFile(path, gid)
    match = plan.grant_for(file_plan_path)
    if match is None:
        raise UnclassifiableFile(path, gid)
    grant_path, groups = match
    if len(groups) > 1:
        raise AmbiguousGrant(path, grant_path, groups)
    return next(iter(groups))
