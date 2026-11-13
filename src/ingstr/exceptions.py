from pathlib import Path


class IngstrError(Exception):
    """Base class for all Ingstr-raised exceptions."""


class ConfigError(IngstrError):
    """Configuration is missing, malformed, or references unreachable resources."""


class PlanError(IngstrError):
    """compiled_plan.yml or group_gid_map.yml is missing, malformed, or inconsistent."""


class UpstreamUnavailable(IngstrError):
    """A required external dependency (Qdrant, Ollama, NFS mount) is unreachable."""


class UnclassifiableFile(IngstrError):
    """A file's filesystem GID is not present in the group→GID map. Fail-closed: skip."""

    def __init__(self, path: Path, gid: int) -> None:
        super().__init__(
            f"{path}: gid {gid} not present in group_gid_map.yml; refusing to index"
        )
        self.path = path
        self.gid = gid


class WrongOrgFile(IngstrError):
    """A file's group belongs to a different org than the one being read.

    Separate from `UnclassifiableFile` on purpose. An unclassifiable file is a
    file the estate has not classified; this is a file the estate *has*
    classified, into an org this run is not indexing. Both are refused, but
    they call for different action — the first for a classification, the second
    for someone to look at why one org's folder holds another org's data — so
    they are reported apart rather than counted together.
    """

    def __init__(self, path: Path, gid: int, group: str, group_org: str, run_org: str) -> None:
        super().__init__(
            f"{path}: group {group!r} belongs to org {group_org!r}, "
            f"but this run is indexing {run_org!r}; refusing to index"
        )
        self.path = path
        self.gid = gid
        self.group = group
        self.group_org = group_org
        self.run_org = run_org


class RecordedFileError(IngstrError):
    """A per-file failure already written to the state database by the step
    that raised it, because only that step knew what to record.

    A parse failure is recorded where the file's classification group is still
    in hand, so a later run can ask "is this the same file, under the same
    image, with the same group?" before deciding not to try again. The caller
    counts it and moves on; it must not record it a second time.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
