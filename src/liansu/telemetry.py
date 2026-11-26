"""T16 · 可观测遥测层 —— append-only JSONL 事件流（DSH 记忆插件的数据源）。

契约（字段名与结构对插件是硬约定，不要随意改）：
- 写入路径：``<vault_root>/.liansu/metrics/events.jsonl``，一行一个 JSON 对象；
- 每行固定字段：``t``（带本地时区偏移的 ISO8601）、``kind``、``session``
  （环境变量 ``LIANSU_SESSION``，未设置则 ``null``）、``pid``、``v``（schema 版本，当前 1），
  调用方以 keyword 传入的业务字段原样平铺在顶层；
- ``LIANSU_TELEMETRY=0``（或 false/off/no）整体禁用，``record`` 变 no-op；
- 任何 IO 异常都被吞掉（只在 stderr 打一行），绝不影响主流程；
- 单文件超过 ``MAX_BYTES``（5 MiB）时轮转为 ``events.1.jsonl``（覆盖旧文件）；
- 进程内用 ``threading.Lock`` 串行化写入，追加用 ``open(..., "a")``（跨进程 append 安全）。

读取侧：``read_events(root)`` 返回解析后的事件列表（坏行忽略）、``iter_events(root)``
为流式版本，二者都只读 ``events.jsonl``（不含轮转归档），供 CLI 聚合使用。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1
MAX_BYTES = 5 * 1024 * 1024
EVENTS_NAME = "events.jsonl"
ROTATED_NAME = "events.1.jsonl"

# 视为「关闭」的 LIANSU_TELEMETRY 取值
_OFF_VALUES = frozenset({"0", "false", "off", "no"})

_LOCK = threading.Lock()


def resolve_root(root: Path | str | None = None) -> Path:
    """解析 vault 根目录：显式参数 > 环境变量 ``LIANSU_ROOT`` > 配置 > 仓库根。

    显式参数优先是为了让调用方能按自己持有的 vault（如测试用 tmp_path、
    CLI 用 --root）落盘，避免所有进程都写同一份事件流。委托 config 统一解析，
    生产默认落 ``config.vault.root``（Obsidian vault），测试仍可用环境变量隔离。
    """
    from liansu.config import resolve_vault_root
    return resolve_vault_root(root)


def metrics_dir(root: Path | str | None = None) -> Path:
    """遥测目录：``<vault_root>/.liansu/metrics``。"""
    return resolve_root(root) / ".liansu" / "metrics"


def events_path(root: Path | str | None = None) -> Path:
    """事件流文件路径：``<vault_root>/.liansu/metrics/events.jsonl``。"""
    return metrics_dir(root) / EVENTS_NAME


def enabled() -> bool:
    """遥测是否开启；``LIANSU_TELEMETRY`` 未设置时默认开启。"""
    raw = os.environ.get("LIANSU_TELEMETRY")
    if raw is None:
        return True
    return raw.strip().lower() not in _OFF_VALUES


def _now_iso() -> str:
    """当前本地时间，ISO8601 带时区偏移（形如 ``2026-11-25T11:20:33+08:00``）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json_default(obj: Any) -> Any:
    """兜底序列化：日期/路径转字符串，其他未知类型也降级为字符串。"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def _rotate_if_needed(path: Path) -> None:
    """当前文件超过阈值时轮转（覆盖旧的 events.1.jsonl）；小于等于阈值不动。"""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= MAX_BYTES:
        return
    os.replace(path, path.with_name(ROTATED_NAME))


def record_at(root: Path | str | None, kind: str, /, **fields: Any) -> None:
    """向指定 vault 追加一条事件（``record`` 的显式根目录版本）。

    与 ``record`` 同一契约，只是跳过 ``LIANSU_ROOT`` 解析：调用方已知 vault 时用它，
    例如 pipeline 用 journal 的上层目录、MCP 用 ctx["root"]，测试据此隔离落盘位置。
    """
    if not enabled():
        return
    try:
        payload: dict[str, Any] = {
            "t": _now_iso(),
            "kind": kind,
            "session": os.environ.get("LIANSU_SESSION") or None,
            "pid": os.getpid(),
            "v": SCHEMA_VERSION,
        }
        payload.update(fields)
        line = json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n"
        path = events_path(root)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(path)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
    except Exception as exc:  # noqa: BLE001 —— 遥测失败绝不影响主流程
        print(f"[liansu] telemetry 写入失败（已忽略）: {exc}", file=sys.stderr)


def record(kind: str, /, **fields: Any) -> None:
    """追加一条遥测事件；永不抛异常、永不阻塞主流程。

    写入 ``<vault_root>/.liansu/metrics/events.jsonl``（vault_root 取 ``LIANSU_ROOT``，
    未设置则仓库根），每行结构见模块 docstring。``LIANSU_TELEMETRY=0`` 时是 no-op。
    """
    record_at(None, kind, **fields)


def iter_events(root: Path | str | None = None) -> Iterator[dict]:
    """流式读 ``events.jsonl``，逐行解析；坏行、非对象行直接跳过。"""
    path = events_path(root)
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:  # noqa: BLE001 —— 坏行（半行写入/手工编辑）忽略
                continue
            if isinstance(event, dict):
                yield event


def read_events(root: Path | str | None = None) -> list[dict]:
    """读取 ``events.jsonl`` 全部事件（按写入顺序）；文件不存在返回空列表。"""
    return list(iter_events(root))


def tail(events: Iterable[dict], n: int) -> list[dict]:
    """取事件序列最后 n 条（新→旧），供聚合层拼 ``recent``。"""
    if n <= 0:
        return []
    return list(events)[-n:][::-1]
