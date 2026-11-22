"""T2 provider 单测：全程 mock openai client，不发真实网络请求。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from openai import APIConnectionError, APITimeoutError

from liansu.providers.api import ApiProvider, ProviderConnectionError, ProviderParseError
from liansu.providers.base import LLMProvider
from liansu.providers.ollama import OllamaProvider
from liansu.schema import Fact

CFG = {
    "llm": {
        "base_url": "http://192.168.0.100:8080/v1",
        "api_key": "dummy",
        "model": "local",
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
}
MEANINGFUL_DIALOG = [
    {"role": "user", "content": "我决定今年跳槽，目标城市定在北京，优先考虑大模型方向。"},
    {"role": "assistant", "content": "收到，已记下你的求职偏好。"},
]
FACT_DICT = Fact.from_yaml(
    (Path(__file__).parent / "fixtures" / "fact.yaml").read_text(encoding="utf-8")
).to_dict()


@pytest.fixture
def openai_ctor():
    """打桩 openai.OpenAI，确保不建立真实连接。"""
    with patch("liansu.providers.api.OpenAI") as ctor:
        yield ctor


def _client(*contents: str) -> MagicMock:
    client = MagicMock()
    create = client.chat.completions.create
    responses = []
    for content in contents:
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content=content))]
        responses.append(resp)
    if len(responses) == 1:
        create.return_value = responses[0]
    else:
        create.side_effect = responses
    return client


# ---- gate：规则前置 + LLM 二段判断 --------------------------------------
def test_gate_true_via_llm(openai_ctor):
    openai_ctor.return_value = _client(
        json.dumps({"category": "长期事实", "worth_remembering": True, "reason": "含求职决策"}, ensure_ascii=False)
    )
    provider = ApiProvider(CFG)
    assert provider.gate(MEANINGFUL_DIALOG) is True

    kwargs = provider._client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "local"
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_gate_false_via_llm(openai_ctor):
    openai_ctor.return_value = _client(
        json.dumps({"category": "闲聊", "worth_remembering": False, "reason": "无长期价值"}, ensure_ascii=False)
    )
    assert ApiProvider(CFG).gate(MEANINGFUL_DIALOG) is False


def test_gate_false_via_rule_chitchat_skips_llm(openai_ctor):
    client = _client("{}")
    openai_ctor.return_value = client
    assert ApiProvider(CFG).gate([{"role": "user", "content": "你好"}]) is False
    client.chat.completions.create.assert_not_called()


def test_gate_false_via_rule_pure_code_skips_llm(openai_ctor):
    client = _client("{}")
    openai_ctor.return_value = client
    dialog = [{"role": "user", "content": "```python\ndef foo():\n    return 1\n```"}]
    assert ApiProvider(CFG).gate(dialog) is False
    client.chat.completions.create.assert_not_called()


# ---- extract：schema 约束 / 校验 / 重试 ----------------------------------
def test_extract_success_with_full_fact_schema(openai_ctor):
    openai_ctor.return_value = _client(json.dumps({"facts": [FACT_DICT]}, ensure_ascii=False))
    provider = ApiProvider(CFG)
    facts = provider.extract(MEANINGFUL_DIALOG, source="codex://rollout-x")

    assert len(facts) == 1
    assert isinstance(facts[0], Fact)
    assert facts[0].id == "F-20261121-001"
    assert facts[0].subject == "梁明晗"

    kwargs = provider._client.chat.completions.create.call_args.kwargs
    fmt = kwargs["response_format"]
    assert fmt["type"] == "json_schema"
    fact_props = fmt["json_schema"]["schema"]["properties"]["facts"]["items"]["properties"]
    assert set(fact_props) == set(Fact.model_fields)  # 覆盖 Fact 全部字段
    assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_extract_retries_once_after_parse_failure(openai_ctor):
    client = _client("not json", json.dumps({"facts": [FACT_DICT]}, ensure_ascii=False))
    openai_ctor.return_value = client
    facts = ApiProvider(CFG).extract(MEANINGFUL_DIALOG)

    assert client.chat.completions.create.call_count == 2
    assert len(facts) == 1 and facts[0].id == "F-20261121-001"


def test_extract_retries_once_after_schema_validation_failure(openai_ctor):
    bad = {**FACT_DICT, "confidence": 1.5}  # 违反 Fact 约束
    client = _client(
        json.dumps({"facts": [bad]}, ensure_ascii=False),
        json.dumps({"facts": [FACT_DICT]}, ensure_ascii=False),
    )
    openai_ctor.return_value = client
    facts = ApiProvider(CFG).extract(MEANINGFUL_DIALOG)
    assert client.chat.completions.create.call_count == 2
    assert facts[0].confidence == 0.92


def test_extract_retry_exhausted_raises(openai_ctor):
    client = _client("garbage", "still garbage")
    openai_ctor.return_value = client
    with pytest.raises(ProviderParseError):
        ApiProvider(CFG).extract(MEANINGFUL_DIALOG)
    assert client.chat.completions.create.call_count == 2


# ---- judge --------------------------------------------------------------
def test_judge_returns_score_and_reason(openai_ctor):
    openai_ctor.return_value = _client(json.dumps({"score": 0.85, "reason": "依据充分"}))
    fact = Fact.from_dict(FACT_DICT)
    result = ApiProvider(CFG).judge("目标城市？", [fact], "北京")
    assert result == {"score": 0.85, "reason": "依据充分"}


# ---- 连接健壮性：明确报错，不静默降级 ------------------------------------
def test_connection_error_raises_provider_connection_error(openai_ctor):
    client = MagicMock()
    client.chat.completions.create.side_effect = APIConnectionError(request=MagicMock())
    openai_ctor.return_value = client
    with pytest.raises(ProviderConnectionError, match="不降级"):
        ApiProvider(CFG).gate(MEANINGFUL_DIALOG)


def test_timeout_raises_provider_connection_error(openai_ctor):
    client = MagicMock()
    client.chat.completions.create.side_effect = APITimeoutError(request=MagicMock())
    openai_ctor.return_value = client
    with pytest.raises(ProviderConnectionError, match="超时"):
        ApiProvider(CFG).gate(MEANINGFUL_DIALOG)


# ---- config 切换不改调用方 ----------------------------------------------
def test_config_switch_does_not_change_caller(openai_ctor):
    openai_ctor.return_value = _client(
        json.dumps({"category": "长期事实", "worth_remembering": True, "reason": "ok"}, ensure_ascii=False)
    )
    cfg_b = {
        "llm": {
            "base_url": "http://192.168.0.200:9000/v1",
            "api_key": "key-b",
            "model": "model-b",
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
    }
    a, b = ApiProvider(CFG), ApiProvider(cfg_b)
    # 同一调用方式，无需因配置变化改任何调用方代码
    assert a.gate(MEANINGFUL_DIALOG) is True
    assert b.gate(MEANINGFUL_DIALOG) is True

    assert openai_ctor.call_args_list[0].kwargs["base_url"] == CFG["llm"]["base_url"]
    assert openai_ctor.call_args_list[1].kwargs["base_url"] == cfg_b["llm"]["base_url"]
    models = [c.kwargs["model"] for c in a._client.chat.completions.create.call_args_list]
    assert models == ["local", "model-b"]


# ---- Ollama 兜底：只指本机 11434 ----------------------------------------
def test_ollama_only_points_localhost(openai_ctor):
    openai_ctor.return_value = _client(
        json.dumps({"category": "长期事实", "worth_remembering": True, "reason": "x"}, ensure_ascii=False)
    )
    provider = OllamaProvider()
    assert isinstance(provider, LLMProvider)
    assert isinstance(provider, ApiProvider)

    ctor_kwargs = openai_ctor.call_args.kwargs
    assert ctor_kwargs["base_url"] == "http://localhost:11434/v1"
    assert "8080" not in json.dumps(ctor_kwargs, ensure_ascii=False)
    assert provider.gate(MEANINGFUL_DIALOG) is True
    assert provider._client.chat.completions.create.call_args.kwargs["extra_body"] == {}
