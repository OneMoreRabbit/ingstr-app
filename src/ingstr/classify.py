from pathlib import Path

from .exceptions import UnclassifiableFile
from .plan import ResolvedPlan


def classify(path: Path, plan: ResolvedPlan) -> str:
    """Resolve a file's filesystem GID to its canonical classification group.

    Raises UnclassifiableFile if the GID is not present in the resolved plan's
    GID→group map. Fail-closed: callers must skip the file, never substitute a
    default group.
    """
    gid = path.stat().st_gid
    try:
        return plan.gid_to_group[gid]
    except KeyError:
        raise UnclassifiableFile(path, gid) from None
