import os
from pathlib import Path

from .exceptions import UnclassifiableFile, WrongOrgFile
from .plan import ResolvedPlan


def classify(
    path: Path,
    plan: ResolvedPlan,
    org: str,
    *,
    stat_info: os.stat_result | None = None,
) -> str:
    """Resolve a file's filesystem GID to its canonical classification group.

    Raises UnclassifiableFile if the GID is not present in the resolved plan's
    GID→group map. Fail-closed: callers must skip the file, never substitute a
    default group.

    Raises WrongOrgFile if the group resolves but belongs to another org. A run
    reads one org's folder; a file there owned by another org's group is a fact
    about the estate worth reporting, not a file to index under this org's
    collection. When the plan carries no `groups[]` section (contract below
    0.11) no org is known for any group and this check cannot fire — the
    caller decides whether to require it.
    """
    # The caller usually has the stat already — it needs size and modified
    # time for the same file in the same breath — so it passes it in rather
    # than making the one syscall twice on every file of every run.
    gid = (stat_info if stat_info is not None else path.stat()).st_gid
    try:
        group = plan.gid_to_group[gid]
    except KeyError:
        raise UnclassifiableFile(path, gid) from None
    group_org = plan.org_of_group(group)
    if group_org is not None and group_org != org:
        raise WrongOrgFile(path, gid, group, group_org, org)
    return group
