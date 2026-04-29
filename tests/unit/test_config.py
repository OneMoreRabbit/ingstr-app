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
