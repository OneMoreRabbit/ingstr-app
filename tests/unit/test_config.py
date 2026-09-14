from pathlib import Path

import pytest

from ingstr.config import IngstrConfig, load_config
from ingstr.exceptions import ConfigError

_MINIMAL_YAML = """\
org: arc
source:
  root: /mnt/raid_arc/drive
plan:
  compiled_plan_path: /mnt/registry/compiled_plan.yml
  group_gid_map_path: /mnt/registry/group_gid_map.yml
embedding:
  endpoint: http://ollama:11434
  model: nomic-embed-text
  vector_dim: 768
qdrant:
  url: http://qdrant_arc:6333
  api_key_env: QDRANT_RW_API_KEY
  collection: documents
state:
  db_path: /var/lib/ingstr/state.db
"""


def test_minimal_config_loads(tmp_yaml):
    path = tmp_yaml("config.yml", _MINIMAL_YAML)
    cfg = load_config(path)
    assert isinstance(cfg, IngstrConfig)
    assert cfg.org == "arc"
    assert cfg.embedding.vector_dim == 768
    assert cfg.chunking.strategy == "unstructured"
    assert cfg.logging.format == "json"


def test_missing_config_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does_not_exist.yml")


def test_invalid_yaml_raises(tmp_yaml):
    path = tmp_yaml("config.yml", "org: arc\n  bad: indent: here")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_missing_required_field_raises(tmp_yaml):
    yaml_text = _MINIMAL_YAML.replace("org: arc\n", "")
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

_AGENT_SOURCE = """
agent_source:
  root: /mnt/agent-hosts/otter
"""


def test_agent_source_is_optional(tmp_yaml):
    """A deployment not yet reading agent surfaces simply omits the section."""
    cfg = load_config(tmp_yaml("c.yml", _MINIMAL_YAML))
    assert cfg.agent_source is None


def test_agent_source_root_has_no_default(tmp_yaml):
    """Declared, never inferred (ADR-0010 §7, constitution §10).

    A wrong-but-plausible root is worse than a missing one: it resolves to a
    real place on the wrong machine and *succeeds*. So the section may be
    absent, but it may not be present-and-vague.
    """
    with pytest.raises(ConfigError):
        load_config(tmp_yaml("c.yml", _MINIMAL_YAML + "\nagent_source:\n  purge_threshold: 0.5\n"))


def test_agent_source_defaults_to_the_three_moved_surfaces(tmp_yaml):
    cfg = load_config(tmp_yaml("c.yml", _MINIMAL_YAML + _AGENT_SOURCE))
    assert cfg.agent_source is not None
    assert cfg.agent_source.root == Path("/mnt/agent-hosts/otter")
    assert cfg.agent_source.surfaces == ["memory", "sessions", "scratch"]
    assert cfg.agent_source.purge_threshold == 0.5


def test_configs_surface_is_refused(tmp_yaml):
    """Refuse loudly rather than silently find nothing.

    `configs/` is agent-private, carries no RBAC group and is not in the export
    (ADR-0010 §1). A config naming it is asking to ingest secrets material, so
    it is rejected rather than quietly yielding an empty walk.
    """
    yaml_text = _MINIMAL_YAML + (
        "\nagent_source:\n"
        "  root: /mnt/agent-hosts/otter\n"
        "  surfaces: [memory, configs]\n"
    )
    with pytest.raises(ConfigError, match="never ingested"):
        load_config(tmp_yaml("c.yml", yaml_text))


@pytest.mark.parametrize("bad", ["0.0", "1.5", "-0.2"])
def test_purge_threshold_bounds(tmp_yaml, bad):
    yaml_text = _MINIMAL_YAML + f"\nagent_source:\n  root: /mnt/x\n  purge_threshold: {bad}\n"
    with pytest.raises(ConfigError):
        load_config(tmp_yaml("c.yml", yaml_text))


def test_explicit_surfaces_are_accepted(tmp_yaml):
    """Covers the validator's accept path, which the default never exercises.

    Pydantic does not run field validators on defaults, so the happy path here
    is only reached when a config states its surfaces explicitly — which is also
    the case an operator narrowing ingestion to one surface would hit.
    """
    yaml_text = _MINIMAL_YAML + (
        "\nagent_source:\n"
        "  root: /mnt/agent-hosts/otter\n"
        "  surfaces: [memory, scratch]\n"
    )
    cfg = load_config(tmp_yaml("c.yml", yaml_text))
    assert cfg.agent_source is not None
    assert cfg.agent_source.surfaces == ["memory", "scratch"]
