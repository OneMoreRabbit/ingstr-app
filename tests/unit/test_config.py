from pathlib import Path

import pytest

from ingstr.config import IngstrConfig, load_config
from ingstr.exceptions import ConfigError

_MINIMAL_YAML = """\
orgs:
  - org: arc
    source:
      root: /mnt/raid_arc/drive
    qdrant:
      url: http://qdrant_arc:6333
      api_key_env: QDRANT_RW_API_KEY
      collection: documents
    state:
      db_path: /var/lib/ingstr/arc/state.db
plan:
  compiled_plan_path: /mnt/registry/compiled_plan.yml
  group_gid_map_path: /mnt/registry/group_gid_map.yml
embedding:
  endpoint: http://ollama:11434
  model: nomic-embed-text
  vector_dim: 768
"""

_TWO_ORGS_YAML = _MINIMAL_YAML.replace(
    "plan:\n",
    """  - org: job
    source:
      root: /mnt/raid_job
    qdrant:
      url: http://qdrant_job:6333
      api_key_env: QDRANT_RW_API_KEY_JOB
      collection: documents
    state:
      db_path: /var/lib/ingstr/job/state.db
plan:\n""",
    1,
)


def test_minimal_config_loads(tmp_yaml):
    path = tmp_yaml("config.yml", _MINIMAL_YAML)
    cfg = load_config(path)
    assert isinstance(cfg, IngstrConfig)
    assert cfg.org_names == ["arc"]
    assert cfg.embedding.vector_dim == 768
    assert cfg.chunking.strategy == "unstructured"
    assert cfg.logging.format == "json"
    # One org configured: --org may be omitted, and the view carries both the
    # org's settings and the shared ones.
    view = cfg.for_org(None)
    assert view.org == "arc"
    assert view.qdrant.collection == "documents"
    assert view.embedding.vector_dim == 768


def test_missing_config_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does_not_exist.yml")


def test_invalid_yaml_raises(tmp_yaml):
    path = tmp_yaml("config.yml", "orgs: arc\n  bad: indent: here")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_missing_required_field_raises(tmp_yaml):
    yaml_text = _MINIMAL_YAML.replace("  - org: arc\n", "")
    path = tmp_yaml("config.yml", yaml_text)
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(path)


def test_unknown_field_is_rejected(tmp_yaml):
    yaml_text = _MINIMAL_YAML + "extra_unknown_field: oops\n"
    path = tmp_yaml("config.yml", yaml_text)
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(path)


def test_non_unstructured_parser_rejected(tmp_yaml):
    yaml_text = _MINIMAL_YAML + "parsers:\n  pdf: custom_parser\n"
    path = tmp_yaml("config.yml", yaml_text)
    with pytest.raises(ConfigError, match="only 'unstructured'"):
        load_config(path)


def test_zero_vector_dim_rejected(tmp_yaml):
    yaml_text = _MINIMAL_YAML.replace("vector_dim: 768", "vector_dim: 0")
    path = tmp_yaml("config.yml", yaml_text)
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(path)


# ── ADR-0010 §7: the agent export root ──────────────────────────────────────

_AGENT_SOURCE_INDENTED = """\
    agent_source:
      root: /mnt/agent-hosts/otter
"""


def _with_agent_source(extra: str = "      root: /mnt/agent-hosts/otter\n") -> str:
    """Put an `agent_source` block inside the arc org entry."""
    return _MINIMAL_YAML.replace(
        "    state:\n      db_path: /var/lib/ingstr/arc/state.db\n",
        "    state:\n      db_path: /var/lib/ingstr/arc/state.db\n"
        "    agent_source:\n" + extra,
        1,
    )


def test_agent_source_is_optional(tmp_yaml):
    """A deployment not yet reading agent surfaces simply omits the section."""
    cfg = load_config(tmp_yaml("c.yml", _MINIMAL_YAML))
    assert cfg.for_org("arc").agent_source is None


def test_agent_source_root_has_no_default(tmp_yaml):
    """Declared, never inferred (ADR-0010 §7, constitution §10).

    A wrong-but-plausible root is worse than a missing one: it resolves to a
    real place on the wrong machine and *succeeds*. So the section may be
    absent, but it may not be present-and-vague.
    """
    with pytest.raises(ConfigError):
        load_config(tmp_yaml("c.yml", _with_agent_source("      purge_threshold: 0.5\n")))


def test_agent_source_defaults_to_the_three_moved_surfaces(tmp_yaml):
    cfg = load_config(tmp_yaml("c.yml", _with_agent_source()))
    agent_source = cfg.for_org("arc").agent_source
    assert agent_source is not None
    assert agent_source.root == Path("/mnt/agent-hosts/otter")
    assert agent_source.surfaces == ["memory", "sessions", "scratch"]
    assert agent_source.purge_threshold == 0.5


def test_configs_surface_is_refused(tmp_yaml):
    """Refuse loudly rather than silently find nothing.

    `configs/` is agent-private, carries no RBAC group and is not in the export
    (ADR-0010 §1). A config naming it is asking to ingest secrets material, so
    it is rejected rather than quietly yielding an empty walk.
    """
    yaml_text = _with_agent_source(
        "      root: /mnt/agent-hosts/otter\n      surfaces: [memory, configs]\n"
    )
    with pytest.raises(ConfigError, match="never ingested"):
        load_config(tmp_yaml("c.yml", yaml_text))


@pytest.mark.parametrize("bad", ["0.0", "1.5", "-0.2"])
def test_purge_threshold_bounds(tmp_yaml, bad):
    yaml_text = _with_agent_source(f"      root: /mnt/x\n      purge_threshold: {bad}\n")
    with pytest.raises(ConfigError):
        load_config(tmp_yaml("c.yml", yaml_text))


def test_explicit_surfaces_are_accepted(tmp_yaml):
    """Covers the validator's accept path, which the default never exercises.

    Pydantic does not run field validators on defaults, so the happy path here
    is only reached when a config states its surfaces explicitly — which is also
    the case an operator narrowing ingestion to one surface would hit.
    """
    yaml_text = _with_agent_source(
        "      root: /mnt/agent-hosts/otter\n"
        "      surfaces: [memory, scratch]\n"
    )
    cfg = load_config(tmp_yaml("c.yml", yaml_text))
    agent_source = cfg.for_org("arc").agent_source
    assert agent_source is not None
    assert agent_source.surfaces == ["memory", "scratch"]


# ── v0.2.0: one configuration, every org ────────────────────────────────────


def test_two_orgs_load_and_resolve_separately(tmp_yaml):
    cfg = load_config(tmp_yaml("c.yml", _TWO_ORGS_YAML))
    assert cfg.org_names == ["arc", "job"]
    arc, job = cfg.for_org("arc"), cfg.for_org("job")
    assert arc.qdrant.url != job.qdrant.url
    assert arc.state.db_path != job.state.db_path
    # Shared settings are the same object's values for both.
    assert arc.embedding.endpoint == job.embedding.endpoint
    assert arc.plan.compiled_plan_path == job.plan.compiled_plan_path


def test_org_is_required_when_several_are_configured(tmp_yaml):
    """§11: the org decides what is read and written. With more than one
    configured it is declared or the tool stops — never the first in the list,
    which is exactly the plausible default nobody audits."""
    cfg = load_config(tmp_yaml("c.yml", _TWO_ORGS_YAML))
    with pytest.raises(ConfigError, match="--org is required"):
        cfg.for_org(None)


def test_unknown_org_names_the_configured_ones(tmp_yaml):
    cfg = load_config(tmp_yaml("c.yml", _TWO_ORGS_YAML))
    with pytest.raises(ConfigError, match="arc, job"):
        cfg.for_org("nope")


def test_duplicate_org_names_are_rejected(tmp_yaml):
    """The second would be unreachable and the first would always win,
    silently."""
    doubled = _TWO_ORGS_YAML.replace("  - org: job", "  - org: arc", 1)
    with pytest.raises(ConfigError, match="duplicate org"):
        load_config(tmp_yaml("c.yml", doubled))


def test_empty_orgs_list_is_rejected(tmp_yaml):
    yaml_text = _MINIMAL_YAML.split("plan:", 1)[1]
    with pytest.raises(ConfigError):
        load_config(tmp_yaml("c.yml", "orgs: []\nplan:" + yaml_text))


def test_two_orgs_sharing_one_collection_are_refused(tmp_yaml):
    """An org's passages stay in their own collection; that is what stops a
    filter mistake crossing orgs. Sharing one would not collide — a passage id
    carries its org — so the points would sit together, correctly keyed and
    wrongly placed, and the first sign would be one org's passage returned to
    the other's reader (dprox, hub #13875)."""
    shared = _TWO_ORGS_YAML.replace("http://qdrant_job:6333", "http://qdrant_arc:6333")
    with pytest.raises(ConfigError, match="own collection"):
        load_config(tmp_yaml("c.yml", shared))


def test_same_qdrant_different_collections_is_allowed(tmp_yaml):
    """The shape the one-search-service design moves toward: one server, one
    collection for each org."""
    one_server = _TWO_ORGS_YAML.replace("http://qdrant_job:6333", "http://qdrant_arc:6333")
    one_server = one_server.replace(
        "      collection: documents\n    state:\n      db_path: /var/lib/ingstr/job/state.db",
        "      collection: documents_job\n    state:\n      db_path: /var/lib/ingstr/job/state.db",
    )
    cfg = load_config(tmp_yaml("c.yml", one_server))
    assert cfg.org_names == ["arc", "job"]


def test_different_qdrant_same_collection_name_is_allowed(tmp_yaml):
    """Today's shape: one container per org, every collection called
    `documents`."""
    cfg = load_config(tmp_yaml("c.yml", _TWO_ORGS_YAML))
    assert [o.qdrant.collection for o in cfg.orgs] == ["documents", "documents"]
