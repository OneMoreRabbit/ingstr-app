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
