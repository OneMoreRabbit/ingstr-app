from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .availability import DEFAULT_PURGE_THRESHOLD
from .exceptions import ConfigError


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path
    #: Where `root` sits beneath the plan's `acl` root, in the plan's own path
    #: form (no leading slash, one trailing slash) — for example `arc/drive/`.
    #:
    #: Needed only to classify by `acl_grants`, and required then. The plan
    #: writes its grant paths relative to a root on beaver; this container
    #: sees its own mount point and nothing that relates the two. Nobody but
    #: the estate knows the answer, because the estate chooses what is
    #: mounted — so it is declared here rather than inferred from the org name
    #: (constitution §10, and ADR-0010 §7's export root, which three
    #: components each invented differently).
    #:
    #: A wrong value does not fail: it matches no grant, or the wrong one. So
    #: a run that needs it and has not been given it stops.
    plan_path: str | None = None
    #: Encodings to try, in order, after UTF-8 fails on a text-ish file.
    #: Empty means UTF-8 only, which is what every version before v0.2.3 did.
    #:
    #: Declared rather than detected on purpose: a guessed encoding produces
    #: confident mojibake, which is embedded and searchable and wrong, and
    #: nothing downstream can tell. A declared one is at least somebody's
    #: decision about their own files.
    fallback_encodings: list[str] = Field(default_factory=list)
    follow_symlinks: bool = False
    exclude_patterns: list[str] = Field(default_factory=list)

    @field_validator("plan_path")
    @classmethod
    def _plan_path_is_in_the_plan_s_form(cls, v: str | None) -> str | None:
        """Written as the plan writes paths, or it matches nothing.

        The joined path is compared against the plan's grant paths as plain
        text. A leading slash or a missing trailing one produces a value that
        looks right and matches nothing — a silent empty result, which is the
        failure this component has spent a fortnight filing against others.
        """
        if v is None:
            return None
        if v.startswith("/") or not v.endswith("/"):
            raise ValueError(
                f"source.plan_path {v!r} must be written as the plan writes "
                "paths: no leading slash, exactly one trailing slash "
                "(e.g. 'arc/drive/')"
            )
        return v


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


class OrgConfig(BaseModel):
    """One organisation: its folder, its collection, its state database.

    The estate runs one deployment with every org listed here, and one
    container per org per run, with only that org's folder mounted (the
    one-search-service design, accepted 2026-11-13). Everything that is
    per-org lives here; everything shared lives on `IngstrConfig`.
    """

    model_config = ConfigDict(extra="forbid")

    org: str
    source: SourceConfig
    agent_source: AgentSourceConfig | None = None
    qdrant: QdrantConfig
    state: StateConfig


class OrgView(BaseModel):
    """One org's settings and the shared settings, resolved into one object.

    The pipeline works on a single org and never chooses between them, so it
    is handed this rather than the whole configuration. Choosing the org is
    `IngstrConfig.for_org`, which happens once, in the CLI.
    """

    model_config = ConfigDict(extra="forbid")

    org: str
    source: SourceConfig
    agent_source: AgentSourceConfig | None
    plan: PlanConfig
    embedding: EmbeddingConfig
    qdrant: QdrantConfig
    chunking: ChunkingConfig
    parsers: dict[str, str]
    state: StateConfig
    logging: LoggingConfig


class IngstrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orgs: list[OrgConfig] = Field(min_length=1)
    plan: PlanConfig
    embedding: EmbeddingConfig
    chunking: ChunkingConfig = ChunkingConfig()
    parsers: dict[str, str] = Field(default_factory=dict)
    logging: LoggingConfig = LoggingConfig()

    @field_validator("orgs")
    @classmethod
    def _org_names_are_unique(cls, v: list[OrgConfig]) -> list[OrgConfig]:
        """Two orgs with one name would make `--org` ambiguous and the second
        unreachable — silently, since the first match would always win."""
        seen: set[str] = set()
        duplicates = sorted({o.org for o in v if o.org in seen or seen.add(o.org)})  # type: ignore[func-returns-value]
        if duplicates:
            raise ValueError(f"duplicate org name(s) in 'orgs': {duplicates}")
        return v

    @field_validator("orgs")
    @classmethod
    def _orgs_do_not_share_a_collection(cls, v: list[OrgConfig]) -> list[OrgConfig]:
        """Two orgs pointing at the same collection on the same Qdrant is
        refused.

        An org's passages stay in their own collection, and that is what makes
        a filter mistake unable to cross orgs. Sharing one would not even show
        up as a collision now that a passage id carries its org — the two orgs'
        points would simply sit together, correctly keyed and wrongly placed,
        and the first sign would be one org's passage returned to the other's
        reader. Cheap to refuse, invisible to find later.

        Same Qdrant, different collections: fine. Different Qdrant, same
        collection name: fine — that is the normal shape.
        """
        seen: dict[tuple[str, str], str] = {}
        for org in v:
            key = (org.qdrant.url, org.qdrant.collection)
            if key in seen:
                raise ValueError(
                    f"orgs {seen[key]!r} and {org.org!r} both write to collection "
                    f"{org.qdrant.collection!r} on {org.qdrant.url} — an org's "
                    "passages must stay in their own collection"
                )
            seen[key] = org.org
        return v

    @property
    def org_names(self) -> list[str]:
        return [o.org for o in self.orgs]

    def for_org(self, name: str | None) -> OrgView:
        """Resolve one org's settings, or fail saying which names exist.

        `name` may be omitted only when exactly one org is configured. With
        several, the org governs what is read and written, so it is declared
        or the tool stops (constitution §11) — never the first in the list.
        """
        if name is None:
            if len(self.orgs) != 1:
                raise ConfigError(
                    "--org is required when the configuration lists more than "
                    f"one org; configured: {', '.join(self.org_names)}"
                )
            chosen = self.orgs[0]
        else:
            matches = [o for o in self.orgs if o.org == name]
            if not matches:
                raise ConfigError(
                    f"no org named {name!r} in the configuration; "
                    f"configured: {', '.join(self.org_names)}"
                )
            chosen = matches[0]
        return OrgView(
            org=chosen.org,
            source=chosen.source,
            agent_source=chosen.agent_source,
            plan=self.plan,
            embedding=self.embedding,
            qdrant=chosen.qdrant,
            chunking=self.chunking,
            parsers=self.parsers,
            state=chosen.state,
            logging=self.logging,
        )

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
