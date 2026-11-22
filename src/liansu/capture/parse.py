"""Codex 会话归档解析：zstd / jsonl → 对话轮次。

返回结构对齐 tests/fixtures/dialog.json 的 turns：
    [{"role": "user" | "assistant", "content": str}, ...]

设计原则：尽力解析、容错优先。单行坏掉、字段变形、系统注入消息都不应
让整份会话解析失败；宁可少提一轮，也不要抛裸异常打断写路径。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

import zstandard

# Codex 会在 user/developer 消息里注入这些系统上下文块，不是真实发言。
_SYSTEM_BLOCK_RE = re.compile(
    r"<(environment_context|permissions instructions|app-context|"
    r"base_instructions|team-context|skills instructions)>.*?</\1>",
    re.DOTALL,
)
_ROLES = ("user", "assistant")


def _strip_system_blocks(text: str) -> str:
    """移除注入的系统上下文块，保留其外的真实文本。"""
    return _SYSTEM_BLOCK_RE.sub("", text).strip()


def _content_to_text(content: object) -> str:
    """把 Codex content（str / list[{type,text}] / dict）尽力拼成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return _strip_system_blocks(content)
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, (list, tuple)):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            txt = item
        elif isinstance(item, dict):
            # 常见承载字段：text / content / output_text / input_text
            txt = (
                item.get("text")
                or item.get("content")
                or item.get("output_text")
                or item.get("input_text")
                or ""
            )
            # 少数结构把文本再包一层 dict
            if isinstance(txt, dict):
                txt = txt.get("text") or txt.get("value") or ""
        else:
            continue
        if not isinstance(txt, str) or not txt.strip():
            continue
        txt = _strip_system_blocks(txt)
        if txt:
            parts.append(txt)
    return "\n".join(parts).strip()


def _extract_role(payload: dict, line_type: object) -> str | None:
    """从 payload 中尽力判定 user/assistant，其他角色（developer 等）忽略。"""
    role = payload.get("role")
    if role in _ROLES:
        return role
    # 没有 role 字段时，从 type 推断：user_message / assistant_message
    for candidate in (payload.get("type"), line_type):
        if isinstance(candidate, str) and candidate.endswith("_message"):
            prefix = candidate[: -len("_message")]
            if prefix in _ROLES:
                return prefix
    return None


def _line_to_turn(line: str) -> dict | None:
    """解析单行 jsonl，命中 user/assistant 消息则返回 turn，否则 None。"""
    if not line.strip():
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None

    line_type = obj.get("type")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        # 兼容整行就是一条 message 对象的情况
        payload = obj

    role = _extract_role(payload, line_type)
    if role is None:
        return None

    content = payload.get("content")
    if content is None:
        content = payload.get("text")
    text = _content_to_text(content)
    if not text:
        return None
    # Codex 把 AGENTS.md 内容以 user 消息注入，不是真实用户发言
    if role == "user" and text.startswith("# AGENTS.md instructions for"):
        return None
    return {"role": role, "content": text}


def _extract_turns(lines: Iterable[str]) -> list[dict]:
    turns: list[dict] = []
    for line in lines:
        turn = _line_to_turn(line)
        if turn is not None:
            turns.append(turn)
    return turns


def parse_jsonl_text(text: str) -> list[dict]:
    """解析一段内存中的 jsonl 文本（供 hook 的 stdin 分支复用）。"""
    return _extract_turns(text.splitlines())


def parse_zst(path: str | Path) -> list[dict]:
    """解压并解析归档 ``rollout-*.jsonl.zst``。"""
    dctx = zstandard.ZstdDecompressor()
    with open(path, "rb") as fh:
        with dctx.stream_reader(fh) as reader:
            data = reader.read().decode("utf-8", "replace")
    return _extract_turns(data.splitlines())


def parse_active(path: str | Path) -> list[dict]:
    """解析 sessions/ 下的活跃文件；若后缀为 .zst 则按压缩归档处理。"""
    path = Path(path)
    if str(path).endswith(".zst"):
        return parse_zst(path)
    with open(path, encoding="utf-8", errors="replace") as fh:
        return _extract_turns(fh)


def parse_rollout(path: str | Path) -> list[dict]:
    """按文件后缀自动选择解析路径的便利入口。"""
    return parse_zst(path) if str(path).endswith(".zst") else parse_active(path)
