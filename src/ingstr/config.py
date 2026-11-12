from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .availability import DEFAULT_PURGE_THRESHOLD
from .exceptions import ConfigError


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path
    follow_symlinks: bool = False
    exclude_patterns: list[str] = Field(default_factory=list)


class AgentSourceConfig(BaseModel):
    """Where this host's agent export is mounted (ADR-0010 §7).

    Optional as a *section*: a deployment that does not yet read agent surfaces
    simply omits it. But `root` has **no default**, because ADR-0010 §7 exists
    precisely because v0.1 left the mount path unnamed and three components each
    invented one — the constitution §10 failure of inferring at the point of use
    what should be declared at the point of ownership. A wrong-but-plausible
    root is worse than a missing one: it resolves to a real place on the wrong
    machine and succeeds.

    The authoritative roots arrive as data in `compiled-rbac-plan` 0.6. This
    stays config-driven until then so adopting 0.6 is a source change rather
    than a behaviour change.
    """

    model_config = ConfigDict(extra="forbid")

    root: Path
    purge_threshold: float = Field(default=DEFAULT_PURGE_THRESHOLD, gt=0.0, le=1.0)
    surfaces: list[str] = Field(default_factory=lambda: ["memory", "sessions", "scratch"])

    @field_validator("surfaces")
    @classmethod
    def _configs_is_never_ingested(cls, v: list[str]) -> list[str]:
        """`configs/` is agent-private and carries no RBAC group (ADR-0010 §1).

        It is not in the export at all, so this cannot normally be reached — but
        a config naming it would be a request to ingest secrets material, and
        that is worth refusing loudly rather than silently finding nothing.
        """
        if "configs" in v:
            raise ValueError(
                "'configs' is never ingested (ADR-0010 §1): it is agent-private, "
                "carries no RBAC classification group, and is not in the export"
            )
        return v


class PlanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compiled_plan_path: Path
    group_gid_map_path: Path
    reload_on_run: bool = True


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str
    model: str
    vector_dim: int = Field(gt=0)
    timeout_seconds: int = Field(default=30, gt=0)
    batch_size: int = Field(default=16, gt=0)


class QdrantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    api_key_env: str
    collection: str
    upsert_batch_size: int = Field(default=64, gt=0)
    timeout_seconds: int = Field(default=30, gt=0)


class ChunkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["unstructured"] = "unstructured"
    chunk_size_chars: int = Field(default=2000, gt=0)
    chunk_overlap_chars: int = Field(default=200, ge=0)


class StateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_path: Path


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "text"] = "json"
    log_full_query: bool = False


class IngstrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org: str
    source: SourceConfig
    agent_source: AgentSourceConfig | None = None
    plan: PlanConfig
    embedding: EmbeddingConfig
    qdrant: QdrantConfig
    chunking: ChunkingConfig = ChunkingConfig()
    parsers: dict[str, str] = Field(default_factory=dict)
    state: StateConfig
    logging: LoggingConfig = LoggingConfig()

    @field_validator("parsers")
    @classmethod
    def _only_unstructured_in_mvp(cls, v: dict[str, str]) -> dict[str, str]:
        bad = {k: parser for k, parser in v.items() if parser != "unstructured"}
        if bad:
            raise ValueError(
                f"only 'unstructured' parser is supported in MVP; got: {bad}"
            )
        return v


DEFAULT_CONFIG_PATH = Path("/etc/ingstr/config.yml")


def load_config(path: Path | None = None) -> IngstrConfig:
    """Load and validate Ingstr config from YAML. Fail fast with clear error on any problem."""
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"config file is not valid YAML: {config_path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping: {config_path}")
    try:
        return IngstrConfig.model_validate(raw)
    except Exception as e:
        raise ConfigError(f"config validation failed: {e}") from e
