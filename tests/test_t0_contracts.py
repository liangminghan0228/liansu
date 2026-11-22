"""T0 契约自检：Fact YAML round-trip + 契约 import + config 环境变量覆盖。"""
from pathlib import Path

import pytest
import yaml

from liansu.config import load_config
from liansu.index.base import Index
from liansu.providers.base import Embedder, LLMProvider
from liansu.schema import Fact

FIXTURES = Path(__file__).parent / "fixtures"


def test_fact_yaml_round_trip():
    raw = (FIXTURES / "fact.yaml").read_text(encoding="utf-8")
    fact = Fact.from_yaml(raw)
    assert fact.id == "F-20261121-001"
    assert fact.subject == "梁明晗"
    assert fact.topics == ["求职", "城市"]
    assert fact.valid_to is None
    # round-trip 一致：再反序列化后所有字段相等
    fact2 = Fact.from_yaml(fact.to_yaml())
    assert fact2.model_dump(mode="json") == fact.model_dump(mode="json")


def test_fact_valid_window():
    f = Fact.from_yaml((FIXTURES / "fact.yaml").read_text(encoding="utf-8"))
    assert f.is_valid_at(f.valid_from) is True
    f2 = f.model_copy(update={"valid_to": f.valid_from})
    assert f2.is_valid_at(f.valid_from) is False  # 左闭右开：valid_to 当天即失效
    from datetime import timedelta
    assert f2.is_valid_at(f.valid_from - timedelta(days=1)) is False
    assert f2.is_valid_at(f.valid_from + timedelta(days=1)) is False


def test_fact_rejects_bad_confidence():
    with pytest.raises(ValueError):
        Fact.from_dict({**yaml.safe_load((FIXTURES / "fact.yaml").read_text(encoding="utf-8")), "confidence": 1.5})


def test_contracts_importable():
    assert all(c is not None for c in (LLMProvider, Embedder, Index, Fact))


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("LIANSU_LLM_BASE_URL", "http://example:9999/v1")
    cfg = load_config()
    assert cfg["llm"]["base_url"] == "http://example:9999/v1"
    assert cfg["confidence_threshold"] == 0.7
