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
    # 契约：id 由系统生成，不采信模型自造（实测模型会给跨批重复的 fact_001）
    assert facts[0].id.startswith("F-") and facts[0].id != FACT_DICT["id"]
    assert facts[0].subject == "梁明晗"
    # 契约：source 由系统覆盖，模型无权决定来源
    assert facts[0].source == "codex://rollout-x"

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
    assert len(facts) == 1 and facts[0].id.startswith("F-")


def test_extract_retries_once_after_malformed_payload(openai_ctor):
    """结构不合法（缺 facts 数组）才重试；字段级瑕疵由 _fill_fact 兜底不重试。"""
    client = _client(
        json.dumps({"wrong_key": []}, ensure_ascii=False),
        json.dumps({"facts": [FACT_DICT]}, ensure_ascii=False),
    )
    openai_ctor.return_value = client
    facts = ApiProvider(CFG).extract(MEANINGFUL_DIALOG)
    assert client.chat.completions.create.call_count == 2
    assert len(facts) == 1


def test_extract_clamps_out_of_range_confidence(openai_ctor):
    """confidence=1.5 这类越界值是常见模型输出，钳制处理，不整批失败。"""
    openai_ctor.return_value = _client(json.dumps({"facts": [{**FACT_DICT, "confidence": 1.5}]}, ensure_ascii=False))
    facts = ApiProvider(CFG).extract(MEANINGFUL_DIALOG)
    assert len(facts) == 1 and facts[0].confidence == 1.0


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
    assert "8080" not in ctor_kwargs["base_url"]
    assert provider.gate(MEANINGFUL_DIALOG) is True
    assert provider._client.chat.completions.create.call_args.kwargs["extra_body"] == {}


def test_fill_fact_completes_system_fields():
    """真实端点踩坑回归：LLM 返回空 id/valid_from/created_at 时系统补全，不抛校验错。"""
    from liansu.providers.api import _fill_fact

    filled = _fill_fact(
        {"subject": "梁明晗", "predicate": "求职目标城市", "object": "北京",
         "statement": "定了北京", "confidence": 0.9, "topics": ["求职"],
         "id": "", "valid_from": "", "valid_to": "", "created_at": "", "source": ""},
        source="smoke://",
    )
    assert filled["id"].startswith("F-")
    assert filled["valid_from"]
    assert filled["valid_to"] is None
    assert filled["created_at"]
    assert filled["source"] == "smoke://"
    from liansu.schema import Fact
    Fact.from_dict(filled)  # 补全后可通过 schema 校验


# ---- _fill_fact：端到端真实链路踩坑的系统侧防御 -------------------------
def test_fill_fact_rejects_hallucinated_dates():
    """模型给无时间信息对话编造 2023-10-01 / valid_to 2025-12-31 → 一律纠正。"""
    from datetime import date
    from liansu.providers.api import _fill_fact

    filled = _fill_fact(
        {"subject": "用户", "predicate": "目标城市", "object": "北京",
         "statement": "目标城市在北京和西安之间还没定", "confidence": 0.8,
         "valid_from": "2023-10-01", "valid_to": "2025-12-31"},
        source="e2e://",
    )
    assert filled["valid_from"] == date.today().isoformat()   # 文本无该年份 → 不采信
    assert filled["valid_to"] is None                          # 新陈述默认当前有效


def test_fill_fact_keeps_date_mentioned_in_text():
    """文本确实写了年份时，valid_from 应保留（如「2024年7月入职」）。"""
    from liansu.providers.api import _fill_fact

    filled = _fill_fact(
        {"subject": "梁明晗", "predicate": "工作单位", "object": "理想汽车",
         "statement": "2024年7月入职理想汽车", "confidence": 1.0,
         "valid_from": "2024-07-01"},
        source="e2e://",
    )
    assert filled["valid_from"] == "2024-07-01"


def test_fill_fact_cleans_null_strings():
    """模型有时把 None 写成字符串 'null'，必须清洗，否则 Date 校验直接失败。"""
    from liansu.schema import Fact
    from liansu.providers.api import _fill_fact

    filled = _fill_fact(
        {"subject": "梁明晗", "predicate": "身高", "object": "188cm",
         "statement": "身高188", "confidence": 0.9,
         "valid_to": "null", "supersedes": "None", "valid_from": "null"},
        source="e2e://",
    )
    assert filled["valid_to"] is None
    assert filled["supersedes"] is None
    Fact.from_dict(filled)  # 校验通过


def test_fill_fact_ids_are_unique_across_calls():
    """模型自造 id 会跨批重复（fact_001 撞 fact_001），系统必须强制换新。"""
    from liansu.providers.api import _fill_fact

    base = {"id": "fact_001", "subject": "梁明晗", "predicate": "身高",
            "object": "188cm", "statement": "身高188", "confidence": 0.9}
    a = _fill_fact(base, source="e2e://")["id"]
    b = _fill_fact(base, source="e2e://")["id"]
    assert a != b and a != "fact_001" and b != "fact_001"
