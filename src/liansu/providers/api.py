"""默认 LLM provider — Windows 主机的 OpenAI-compatible /v1 服务（T2）。

连接参数（base_url / api_key / model / extra_body）一律从 config + 环境变量读取，
端点不硬编码。gate / extract / judge 共用同一条 OpenAI SDK 通路；
主机不可达或超时时抛明确异常，绝不静默降级到别的 provider。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

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


_NULLISH = {"", "null", "none", "nil", "n/a", "na", "undefined", "-"}


def _clean_scalar(value: Any) -> Any:
    """把模型爱吐的 'null'/'None'/'' 等字符串统一成 None。"""
    if isinstance(value, str) and value.strip().lower() in _NULLISH:
        return None
    return value


def _coerce_date(value: Any) -> date | None:
    """尽力把模型给的日期解析成 date；解析不出返回 None。"""
    value = _clean_scalar(value)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) >= 10:
        text = text[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _trusted_valid_from(raw_from: Any, statement: str) -> str:
    """valid_from 只在「文本里真提到过这个年份」时才采信模型的值。

    实测模型会给无时间信息的对话编造 '2023-10-01' / '2025-12-31'，导致刚写入
    就显示过期。凡文本未提及的年份一律视为幻觉，回退到当天。
    """
    parsed = _coerce_date(raw_from)
    today = date.today()
    if parsed is None or parsed > today:
        return today.isoformat()
    if str(parsed.year) not in (statement or ""):
        return today.isoformat()
    return parsed.isoformat()


def _fill_fact(raw: dict[str, Any], source: str) -> dict[str, Any]:
    """系统元数据不由 LLM 决定，一律由系统补全或覆盖。

    真实端点端到端实测踩到的坑（mock 永远测不出）：
      1. id 交模型自造会跨批重复（fact_001 撞 fact_001），主键冲突丢数据；
      2. valid_from/valid_to 是幻觉，新事实一写入就「已过期」；
      3. valid_to 可能是字符串 'null'，Date 校验直接失败；
      4. source 被模型覆盖成「用户对话」，丢掉调用方的真实来源。
    """
    raw = dict(raw)
    statement = str(raw.get("statement") or "")

    # 1) 主键与来源：无条件系统生成，绝不采信模型
    raw["id"] = f"F-{date.today():%Y%m%d}-{uuid4().hex[:8]}"
    raw["source"] = source or raw.get("source") or "llm://unknown"
    raw["created_at"] = datetime.now(timezone.utc).isoformat()

    # 2) 时间：valid_from 需文本佐证；valid_to 一律清空（新陈述默认当前有效）
    raw["valid_from"] = _trusted_valid_from(raw.get("valid_from"), statement)
    raw["valid_to"] = None

    # 3) 数值与集合
    conf = raw.get("confidence")
    try:
        conf = float(conf) if conf is not None else 0.7
    except (TypeError, ValueError):
        conf = 0.7
    raw["confidence"] = min(1.0, max(0.0, conf))

    topics = raw.get("topics")
    if isinstance(topics, str):
        topics = [topics] if topics.strip() else []
    raw["topics"] = [str(t) for t in (topics or []) if str(t).strip()]

    # 4) 字符串字段兜底，避免 None / 'None' 打穿校验
    for key in ("subject", "predicate", "object", "statement"):
        if raw.get(key) is None:
            raw[key] = ""
    raw["supersedes"] = _clean_scalar(raw.get("supersedes"))
    return raw


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
        # 局域网端点默认不走系统代理：若环境有 HTTP_PROXY 且 NO_PROXY 未含该 IP，
        # SDK 会把请求发进代理导致 502。外网 API 可显式设 trust_env: true。
        self.trust_env: bool = bool(llm.get("trust_env", False))

        # Qwen3 必须关 thinking，否则 JSON 外混入思考文本；config 可加其他 extra 项，
        # 但 enable_thinking=False 不可丢。
        extra = llm.get("extra_body")
        self._extra_body: dict[str, Any] = dict(extra) if isinstance(extra, dict) else {}
        self._extra_body.setdefault("chat_template_kwargs", {})["enable_thinking"] = False

        if client is not None:
            self._client = client
        else:
            kwargs: dict[str, Any] = dict(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
            try:
                import httpx2
                kwargs["http_client"] = httpx2.Client(trust_env=self.trust_env, timeout=self.timeout)
            except Exception:
                pass  # 不同 openai 版本依赖名不同，退回默认 client
            self._client = OpenAI(**kwargs)

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
                    "你是连溯的结构化事实抽取器。从对话中抽取值得长期记忆的事实，只输出 JSON。\n"
                    "字段规范（必须严格遵守，否则下游的时效替换会失效）：\n"
                    "1. subject：事实主体，必须是稳定实体名。指用户本人时一律写「用户」"
                    "（不要写「我」「本人」或具体姓名）；禁止把属性名/话题当 subject（错误：「目标城市」「职业规划」）。\n"
                    "2. predicate：稳定属性名，2~6 字的名词或动宾短语（如「求职目标城市」「毕业院校」「身高」）。"
                    "禁止把句子片段当 predicate（错误：「在...之间还没定下来」「确定为」「准备转」）。"
                    "同一 subject 的同一属性，前后必须用完全相同的写法。\n"
                    "3. object：属性取值，尽量简短（「北京」而非「北京和西安之间还没定下来」）。\n"
                    "4. statement：保留原始自然语言，可引用原句。\n"
                    "5. confidence：0~1。用户明确陈述 ≥0.9；推理猜测 ≤0.6。\n"
                    "6. topics：1~3 个主题标签。\n"
                    "7. id / valid_from / valid_to / created_at / source 由系统填写，一律给 null。\n"
                    "示例输入「我想好了，目标城市就定北京，西安不考虑了」→\n"
                    '{"facts":[{"subject":"用户","predicate":"求职目标城市","object":"北京",'
                    '"statement":"目标城市就定北京，西安不考虑了","confidence":0.95,'
                    '"topics":["求职"],"id":null,"valid_from":null,"valid_to":null,'
                    '"created_at":null,"source":null,"supersedes":null}]}'
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
                    facts.append(Fact.from_dict(_fill_fact(raw, source)))
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
