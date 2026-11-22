"""Stop hook 入口：把 hook stdin（或降级会话文件）转成对话轮次。

对外只暴露 capture()，它保证：
- 不向调用方抛裸异常，任何失败都返回结构化 dict（含 code / error）；
- 失败时写明确 error 日志，便于 hook 场景下排查；
- stdin 拿不到对话时降级读最新归档/活跃会话文件（目录可注入，便于测试）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .parse import parse_active, parse_jsonl_text

logger = logging.getLogger("liansu.capture")

DEFAULT_ARCHIVED_DIR = Path.home() / ".codex" / "archived_sessions"
DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# 结构化返回码
CODE_OK = "OK"
CODE_NO_ROLLOUT = "NO_ROLLOUT"
CODE_PARSE_ERROR = "PARSE_ERROR"
CODE_EMPTY_DIALOG = "EMPTY_DIALOG"


def _result(
    ok: bool,
    turns: list[dict] | None = None,
    source: str | None = None,
    code: str = CODE_OK,
    error: str | None = None,
) -> dict:
    return {
        "ok": ok,
        "turns": turns or [],
        "source": source,
        "code": code,
        "error": error,
    }


def _normalize_turns(items: object) -> list[dict]:
    """把 list[dict] 规范化成 turns；字段变形时跳过而非报错。"""
    if not isinstance(items, list):
        return []
    turns: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                (
                    (part.get("text") or part.get("content") or "")
                    if isinstance(part, dict)
                    else str(part)
                )
                for part in content
            )
        if not isinstance(content, str) or not content.strip():
            continue
        turns.append({"role": role, "content": content.strip()})
    return turns


def _parse_stdin(stdin_text: str) -> tuple[list[dict], str]:
    """尽力从 stdin 解析对话，返回 (turns, 诊断标签)。

    stdin 可能是：
      1. 一段 jsonl 文本（直接是 rollout 行）；
      2. hook payload JSON，内嵌 turns/messages/dialog，或带 transcript_path；
      3. 纯文本（兜底当作一条 user 发言）。
    """
    text = stdin_text.strip()
    if not text:
        return [], "empty"

    # 1) 直接当 jsonl 解析
    turns = parse_jsonl_text(text)
    if turns:
        return turns, "jsonl"

    # 2) 当 JSON（hook payload / 对话对象）解析
    payload: object = None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    if isinstance(payload, dict):
        for key in ("turns", "messages", "dialog", "conversation"):
            extracted = _normalize_turns(payload.get(key))
            if extracted:
                return extracted, f"field:{key}"
        for key in ("transcript_path", "transcript", "session_file", "path"):
            path = payload.get(key)
            if isinstance(path, str) and path:
                try:
                    extracted = parse_active(path)
                    if extracted:
                        return extracted, f"file:{key}"
                except (OSError, ValueError):
                    logger.debug("stdin 指定的会话文件读取失败: %s", path)
        # 是合法 JSON 但没有对话字段：典型的 hook 元数据 payload，不伪造成对话
        return [], "hook-payload-no-dialog"

    if isinstance(payload, list):
        extracted = _normalize_turns(payload)
        if extracted:
            return extracted, "list"

    # 3) 纯文本兜底
    return [{"role": "user", "content": text}], "plain-text"


def _list_rollouts(archived_dir: Path, sessions_dir: Path) -> list[Path]:
    """收集候选会话文件（压缩归档 + sessions 活跃文件），按 mtime 倒序。"""
    candidates: list[Path] = []
    patterns = (
        (archived_dir, "*.jsonl.zst"),
        (sessions_dir, "**/*.jsonl"),
        (sessions_dir, "**/*.jsonl.zst"),
    )
    for directory, pattern in patterns:
        if directory and directory.is_dir():
            candidates.extend(directory.glob(pattern))
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def capture(
    stdin_text: str | None = None,
    *,
    source_path: str | Path | None = None,
    archived_dir: str | Path | None = None,
    sessions_dir: str | Path | None = None,
) -> dict:
    """捕获一次会话对话。

    优先级：显式 source_path > stdin > 最新归档/活跃文件降级。

    Args:
        stdin_text: Stop hook 的 stdin 原文；为 None/空时走降级。
        source_path: CLI --source 显式指定的会话文件。
        archived_dir: 归档目录，默认 ~/.codex/archived_sessions。
        sessions_dir: 活跃会话目录，默认 ~/.codex/sessions。

    Returns:
        dict{ok, turns, source, code, error}，永不抛裸异常。
    """
    archived = Path(archived_dir) if archived_dir else DEFAULT_ARCHIVED_DIR
    sessions = Path(sessions_dir) if sessions_dir else DEFAULT_SESSIONS_DIR

    # 1) 显式文件路径（CLI --source）
    if source_path:
        try:
            turns = parse_active(source_path)
        except Exception as exc:  # noqa: BLE001 - hook 场景必须兜住所有异常
            logger.error("读取指定会话文件失败 path=%s err=%s", source_path, exc, exc_info=True)
            return _result(False, code=CODE_PARSE_ERROR, error=f"{source_path}: {exc}")
        if turns:
            return _result(True, turns, str(source_path), CODE_OK)
        logger.error("指定会话文件未解析出对话 path=%s", source_path)
        return _result(False, code=CODE_EMPTY_DIALOG, error=f"{source_path}: 未解析出对话")

    # 2) stdin 优先
    if stdin_text is not None and stdin_text.strip():
        turns, via = _parse_stdin(stdin_text)
        if turns:
            return _result(True, turns, f"stdin:{via}", CODE_OK)
        logger.warning("hook stdin 未解析出对话（诊断=%s），转入降级", via)
    else:
        logger.info("hook stdin 为空，转入降级")

    # 3) 降级：最新归档 / 活跃会话文件
    try:
        candidates = _list_rollouts(archived, sessions)
    except OSError as exc:
        logger.error("枚举会话目录失败 archived=%s sessions=%s err=%s", archived, sessions, exc)
        return _result(False, code=CODE_PARSE_ERROR, error=f"枚举会话文件失败: {exc}")

    if not candidates:
        logger.error("降级失败：未找到任何会话文件 archived=%s sessions=%s", archived, sessions)
        return _result(False, code=CODE_NO_ROLLOUT, error="未找到任何会话文件")

    last_error = ""
    for path in candidates[:3]:  # 逐个尝试，容忍个别文件损坏
        try:
            turns = parse_active(path)
        except Exception as exc:  # noqa: BLE001 - 单个候选失败不应中断降级
            last_error = f"{path}: {exc}"
            logger.warning("降级候选解析失败 path=%s err=%s", path, exc)
            continue
        if turns:
            return _result(True, turns, str(path), CODE_OK)
        last_error = f"{path}: 未解析出对话"
        logger.warning("降级候选未解析出对话 path=%s", path)

    logger.error("降级失败：%d 个候选均不可用，最后错误=%s", len(candidates[:3]), last_error)
    return _result(False, code=CODE_PARSE_ERROR, error=last_error)
