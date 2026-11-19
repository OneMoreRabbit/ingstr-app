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
    """Where beaver mounts the agents' folders (`ingstr-deploy` 0.8).

    Optional as a *section*: an org with no agent folders omits it and nothing
    about that org changes. `root` has **no default**, because ADR-0010 §7
    exists precisely because v0.1 left the mount path unnamed and three
    components each invented one — the constitution §10 failure of inferring at
    the point of use what should be declared at the point of ownership. A
    wrong-but-plausible root is worse than a missing one: it resolves to a real
    place on the wrong machine and succeeds.

    **`root` changed meaning in 0.8.** It was this host's export mount,
    `/mnt/agent-hosts/<host>/`. It is now the root every path the plan declares
    is relative to, `/mnt/agent-hosts/`, because `compiled-rbac-plan` 0.12
    writes `<host>/` as the first segment of each path. A root with a host
    segment still in it matches no declared path and reads nothing — silently,
    which is why the check below is worth its lines.
    """

    model_config = ConfigDict(extra="forbid")

    root: Path
    #: Both refused rather than ignored. They are declared so the refusal can
    #: name where the value went; `extra="forbid"` alone would say only
    #: "unexpected key", which tells a deployment nothing about what to do.
    surfaces: list[str] | None = None
    purge_threshold: float | None = None

    @field_validator("surfaces")
    @classmethod
    def _the_plan_declares_the_folders_now(cls, v: list[str] | None) -> None:
        """Which folders exist is the plan's to say, not a deployment's.

        Until 0.8 this list chose them. `compiled-rbac-plan` 0.12 declares every
        agent folder with its group and whether it is indexed, so a second
        declaration here could only ever disagree with the first — and the way
        two declarations of one thing fail is silently, with each side sure it
        is right.
        """
        if v is None:
            return None
        raise ValueError(
            "agent_source.surfaces is no longer read: compiled-rbac-plan 0.12 "
            "declares every agent folder and whether it is indexed. Delete the "
            "key — a folder is indexed when the plan says so, and 'configs' "
            "never is"
        )

    @field_validator("purge_threshold")
    @classmethod
    def _purge_threshold_belongs_to_the_org(cls, v: float | None) -> None:
        """It governs the whole org, so it is declared on the org (0.8).

        It has sat here since v0.1.1 while governing the blast-radius guard for
        everything the org indexes (ADR-0010 §6 layer 3) — so an org with no
        agent folders could not set it at all, and an org that added them would
        silently change the bar for its ordinary data.
        """
        if v is None:
            return None
        raise ValueError(
            "agent_source.purge_threshold moved to the org in ingstr-deploy "
            "0.8: declare `purge_threshold:` beside `source:`. It governs the "
            "removal guard for everything this org indexes, not only agent "
            "folders"
        )


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
    #: Refuse a removal that would take more than this share of what this org
    #: knows (ADR-0010 §6 layer 3). Declared on the org because that is what it
    #: governs; it lived in `agent_source` until ingstr-deploy 0.8.
    purge_threshold: float = Field(default=DEFAULT_PURGE_THRESHOLD, gt=0.0, le=1.0)
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
    purge_threshold: float
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
            purge_threshold=chosen.purge_threshold,
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
