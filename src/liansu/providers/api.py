"""默认 LLM provider — Windows 主机的 OpenAI-compatible /v1 服务（T2）。

连接参数（base_url / api_key / model / extra_body）一律从 config + 环境变量读取，
端点不硬编码。gate / extract / judge 共用同一条 OpenAI SDK 通路；
主机不可达或超时时抛明确异常，绝不静默降级到别的 provider。
"""
from __future__ import annotations

import json
import re
from typing import Any

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

from liansu.config import get_llm_config
from liansu.providers.base import LLMProvider
from liansu.schema import Fact

__all__ = [
    "ApiProvider",
    "ProviderError",
    "ProviderConnectionError",
    "ProviderParseError",
    "FACT_SCHEMA",
    "EXTRACT_SCHEMA",
]

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2

# JSON Schema 覆盖 Fact 全部字段（TECH_DESIGN §3），供 response_format 使用。
FACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "事实唯一 id，如 F-20261121-001"},
        "subject": {"type": "string", "description": "主体（人名/项目/实体）"},
        "predicate": {"type": "string", "description": "关系或属性，如 求职目标城市"},
        "object": {"type": "string", "description": "客体值"},
        "statement": {"type": "string", "description": "原始自然语言证据"},
        "valid_from": {"type": "string", "description": "事实生效日 YYYY-MM-DD"},
        "valid_to": {"type": ["string", "null"], "description": "失效日 YYYY-MM-DD；null=仍成立"},
        "confidence": {"type": "number", "description": "置信度 [0,1]"},
        "topics": {"type": "array", "items": {"type": "string"}, "description": "主题标签"},
        "source": {"type": "string", "description": "来源标识，如 codex://rollout-..."},
        "created_at": {"type": "string", "description": "入库时间 ISO-8601，带时区"},
        "supersedes": {"type": ["string", "null"], "description": "被覆盖的旧 fact id；null=无"},
    },
    "required": [
        "id", "subject", "predicate", "object", "statement", "valid_from",
        "valid_to", "confidence", "topics", "source", "created_at", "supersedes",
    ],
    "additionalProperties": False,
}

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": FACT_SCHEMA}},
    "required": ["facts"],
    "additionalProperties": False,
}

_GATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "description": "第一步：对话类型（代码改动/闲聊/长期事实）"},
        "worth_remembering": {"type": "boolean", "description": "第二步：是否值得长期记忆"},
        "reason": {"type": "string"},
    },
    "required": ["category", "worth_remembering", "reason"],
    "additionalProperties": False,
}

_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "description": "0..1 的质量分"},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
    "additionalProperties": False,
}

# 规则前置：这些对话直接判 False，不调用 LLM，省成本。
_CHITCHAT_RE = re.compile(
    r"^\s*(你好|您好|hi|hello|hey|thanks|thank you|谢谢|多谢|再见|bye|goodbye|"
    r"晚安|早上好|晚上好|哈哈+|hmm|ok|好的|收到|明白|了解|嗯+)[。!！？?.,，~\s]*$",
    re.IGNORECASE,
)
_CODE_LINE_RE = re.compile(
    r"^\s*(```|~~~|diff --git|@@|[-+]{3}|[-+]\s|def\s|class\s|import\s|from\s|"
    r"if\s|for\s|while\s|return\s|[{}\[\]();]|\*|/|#|$)"
)


class ProviderError(RuntimeError):
    """provider 层统一异常基类。"""


class ProviderConnectionError(ProviderError):
    """主机不可达 / 超时；不降级，调用方必须显式处理。"""


class ProviderParseError(ProviderError):
    """LLM 输出不符合约定 JSON 或 Fact schema。"""


def _json_schema_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": schema},
    }


def _strip_fences(text: str) -> str:
    """去掉模型偶发包上的 ```json 围栏。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


class ApiProvider(LLMProvider):
    """OpenAI-compatible /v1 provider（默认实现）。

    Args:
        config: 完整 config dict；None 时从 config.yaml 加载（含环境变量覆盖）。
        client: 可选的已有 OpenAI client，便于测试注入 mock。
    """

    def __init__(self, config: dict[str, Any] | None = None, *, client: Any | None = None):
        llm = get_llm_config(config)
        base_url, api_key, model = (
            llm.get("base_url"),
            llm.get("api_key"),
            llm.get("model"),
        )
        if not all(isinstance(v, str) and v for v in (base_url, api_key, model)):
            raise ValueError("llm 配置缺少 base_url / api_key / model，请检查 config.yaml 或环境变量")

        self.base_url: str = base_url
        self.api_key: str = api_key
        self.model: str = model
        self.timeout: float = float(llm.get("timeout", DEFAULT_TIMEOUT))
        self.max_retries: int = int(llm.get("max_retries", DEFAULT_MAX_RETRIES))

        # Qwen3 必须关 thinking，否则 JSON 外混入思考文本；config 可加其他 extra 项，
        # 但 enable_thinking=False 不可丢。
        extra = llm.get("extra_body")
        self._extra_body: dict[str, Any] = dict(extra) if isinstance(extra, dict) else {}
        self._extra_body.setdefault("chat_template_kwargs", {})["enable_thinking"] = False

        self._client = client if client is not None else OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    # ---- 底层调用 -------------------------------------------------------
    def _chat(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        name: str,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": _json_schema_format(name, schema),
            "extra_body": self._extra_body,  # Qwen3: enable_thinking=false
        }
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except (APIConnectionError, APITimeoutError) as exc:
            raise ProviderConnectionError(
                f"LLM 服务不可达或超时 ({self.base_url})，不降级到其他 provider: {exc}"
            ) from exc
        except APIError as exc:
            raise ProviderError(f"LLM 服务返回错误 ({self.base_url}): {exc}") from exc

        try:
            content = resp.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ProviderParseError("LLM 响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise ProviderParseError("LLM 响应内容为空")
        return content

    @staticmethod
    def _dialog_payload(dialog: list[dict[str, str]]) -> str:
        return json.dumps(dialog, ensure_ascii=False)

    # ---- LLMProvider 接口 -----------------------------------------------
    def gate(self, dialog: list[dict[str, str]]) -> bool:
        """规则前置 + LLM 二段判断，输出 bool。"""
        if self._rule_skip(dialog):
            return False

        messages = [
            {
                "role": "system",
                "content": (
                    "你是连溯记忆系统的门控器。分两步："
                    "1) 在 category 中标注对话类型（纯代码改动 / 闲聊 / 长期事实）；"
                    "2) 仅当对话包含值得长期记住的用户画像、偏好、决策、计划或约束时，"
                    "令 worth_remembering 为 true，否则为 false。只输出 JSON。"
                ),
            },
            {"role": "user", "content": self._dialog_payload(dialog)},
        ]
        content = self._chat(messages, _GATE_SCHEMA, "gate")
        try:
            data = json.loads(_strip_fences(content))
            return bool(data["worth_remembering"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ProviderParseError(f"gate 输出解析失败: {exc}") from exc

    def extract(self, dialog: list[dict[str, str]], source: str = "") -> list[Fact]:
        """按 Fact schema 抽取 facts[]；JSON/校验失败重试一次。"""
        messages = [
            {
                "role": "system",
                "content": (
                    "你是连溯的结构化事实抽取器。从对话中抽取值得长期记忆的事实，"
                    "每条严格按给定 schema 填写所有字段；无对应值时用 null 或空字符串。"
                    "statement 保留原始自然语言。只输出 JSON。"
                ),
            },
            {"role": "user", "content": self._dialog_payload(dialog)},
        ]
        last_exc: Exception | None = None
        for _ in range(2):  # 首次 + 重试一次
            content = self._chat(messages, EXTRACT_SCHEMA, "facts")
            try:
                data = json.loads(_strip_fences(content))
                raw_facts = data["facts"]
                if not isinstance(raw_facts, list):
                    raise ValueError("facts 必须是数组")
                facts: list[Fact] = []
                for raw in raw_facts:
                    if not isinstance(raw, dict):
                        raise ValueError("每条 fact 必须是对象")
                    if source and not raw.get("source"):
                        raw = {**raw, "source": source}
                    facts.append(Fact.from_dict(raw))
                return facts
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                last_exc = exc
        raise ProviderParseError(f"extract 输出解析失败（已重试一次）: {last_exc}")

    def judge(self, question: str, facts: list[Fact], answer: str) -> dict:
        """LLM-as-Judge / G-Eval 打分，返回含 score / reason 的 dict。"""
        payload = {
            "question": question,
            "facts": [f.to_dict() for f in facts],
            "answer": answer,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是评测裁判。依据给定事实判断答案质量，score 为 0..1 的浮点数，"
                    "并在 reason 中给出简短依据。只输出 JSON。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        content = self._chat(messages, _JUDGE_SCHEMA, "judge")
        try:
            data = json.loads(_strip_fences(content))
            score = data["score"]
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0.0 <= score <= 1.0:
                raise ValueError("score 必须是 [0,1] 的数值")
            return {"score": float(score), "reason": str(data.get("reason", ""))}
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ProviderParseError(f"judge 输出解析失败: {exc}") from exc

    # ---- 规则前置 -------------------------------------------------------
    @staticmethod
    def _rule_skip(dialog: list[dict[str, str]]) -> bool:
        """纯代码改动 / 闲聊直接 False，不调用 LLM。"""
        user_texts = [str(m.get("content", "")) for m in dialog if m.get("role") == "user"]
        if not user_texts:
            return False
        if all(_CHITCHAT_RE.match(t) for t in user_texts if t.strip()):
            return True
        # 纯代码改动：每个用户消息去掉代码围栏后只剩代码/diff 行，没有散文。
        for text in user_texts:
            if not text.strip():
                continue
            outside: list[str] = []
            in_fence = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("```") or stripped.startswith("~~~"):
                    in_fence = not in_fence
                    continue
                if not in_fence and stripped and not _CODE_LINE_RE.match(stripped):
                    return False
        return True
