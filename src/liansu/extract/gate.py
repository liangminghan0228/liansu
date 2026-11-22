"""T4 · 值得记判断（gate）。便宜规则前置，通过后再交给 provider 做 LLM 二段判断。

规则层只做「明显不值得记」的快速否决，省 LLM 成本：
- 空/极短对话
- 纯代码改动、纯闲聊寒暄、纯报错日志
"""
from __future__ import annotations

import re

from liansu.providers.base import LLMProvider

# 明显不值得长期记忆的模式
_CHITCHAT = re.compile(r"^(好的|收到|谢谢|嗯+|ok|okay|你好|hi|hello|再见)[。.!！~ ]*$", re.I)
_CODEISH = re.compile(r"^\s*(def |class |import |from |function |const |let |var |#|\$ |>>> )", re.M)

MIN_CHARS = 8


def _text_of(dialog: list[dict[str, str]]) -> str:
    return "\n".join(t.get("content", "") for t in dialog if isinstance(t, dict))


def rule_gate(dialog: list[dict[str, str]]) -> bool | None:
    """返回 False=明确不值得记；True=规则放行；None=规则无意见（交 LLM）。"""
    text = _text_of(dialog).strip()
    if len(text) < MIN_CHARS:
        return False
    user_turns = [t.get("content", "").strip() for t in dialog if t.get("role") == "user"]
    if user_turns and all(_CHITCHAT.match(u) for u in user_turns if u):
        return False
    # 全是代码块且无自然语言描述
    non_code = "\n".join(
        line for line in text.splitlines() if line.strip() and not _CODEISH.match(line)
    )
    if len(non_code.strip()) < MIN_CHARS:
        return False
    return None


def worth_recording(dialog: list[dict[str, str]], provider: LLMProvider | None = None) -> bool:
    """规则 -> provider.gate 两段式。provider 缺省时只走规则（None 视为放行）。"""
    verdict = rule_gate(dialog)
    if verdict is False:
        return False
    if provider is None:
        return True
    return bool(provider.gate(dialog))
