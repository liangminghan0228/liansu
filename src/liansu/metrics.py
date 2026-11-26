"""T16 · 指标聚合层 —— 把 telemetry 事件流折成 CLI / DSH 插件契约的 JSON。

只读不写：输入是 ``<vault_root>/.liansu/metrics/events.jsonl``，输出是固定结构
（字段名是给插件的硬契约，缺数据给 0 / null / []）：

- ``global``：窗口内全量摘要（counts / extract / recall / latency / daily / lastBrief）；
- ``session``：给了 ``--session`` 时是对该 session 的同结构摘要（不含 vault），否则 null；
- ``vault``：journal 真源与 ``vault_views/`` 的当前体量（读不到给 0）；
- ``recent``：窗口内最近 30 条事件，倒序（新→旧），每条带中文短句 summary，
  以及按类型抽取的 ``detail``，供插件展开明细。``detail`` 里的 id 会就地解析成**事实正文**
  （statement / 三元组 / 置信度 / 有效期 / 主题 / 来源，找不到的 id 标 ``found: false``），
  单条事件最多 ``DETAIL_FACT_LIMIT`` 条、其余用 ``omitted`` 计数；抽取给
  ``writtenFacts`` / ``supersededFacts``，召回与扩散给 ``facts``（召回额外带分数）。

窗口口径：最近 ``days`` 天（含今天），按事件时间戳的本地日期。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from liansu import telemetry

KINDS = ("extract", "recall", "expand", "brief", "rebuild")
LATENCY_KINDS = ("extract", "recall", "brief")
RECENT_LIMIT = 30
DETAIL_FACT_LIMIT = 10   # 单条事件最多附带多少条事实正文，避免 recent 体量失控

JOURNAL_DIR = "journal"
VIEWS_DIR = "vault_views"
ENTITIES_DIR = "entities"
TOPICS_DIR = "topics"


# ---------------------------------------------------------------- 基础工具


def _event_time(event: dict) -> datetime | None:
    """解析事件 ``t`` 字段；缺失或非法返回 None。"""
    raw = event.get("t")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _event_date(event: dict) -> date | None:
    """事件时间戳对应的本地日期（``t`` 自带时区偏移，取壁钟日期即本地日期）。"""
    dt = _event_time(event)
    return dt.date() if dt is not None else None


def _id_list(value: Any) -> list[str]:
    """把 ``written`` / ``ids`` 字段规整为 id 列表；非法值退化为空列表。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _count(value: Any) -> int:
    """``superseded`` 既可能是 id 列表也可能是数量，统一取条数。"""
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _num(value: Any) -> float | None:
    """数值型字段校验：非数值（含 bool/None）返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def percentile(values: Iterable[float], q: float) -> float:
    """线性插值百分位（q ∈ [0,1]）；无样本返回 0。"""
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * max(0.0, min(1.0, q))
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _pct(values: list[float], q: float) -> float:
    """输出用百分位：保留 1 位小数，避免浮点噪声进插件。"""
    return round(percentile(values, q), 1)


# ---------------------------------------------------------------- 窗口 / 摘要


def _in_window(event: dict, today: date, days: int) -> bool:
    """事件是否落在最近 days 天（含今天）；时间戳坏掉的事件不算。"""
    day = _event_date(event)
    if day is None:
        return False
    return today - timedelta(days=days - 1) <= day <= today


def _empty_summary(days: int, today: date) -> dict:
    """空摘要骨架：daily 仍覆盖窗口内每一天（缺数据给 0）。"""
    daily = [
        {"date": (today - timedelta(days=offset)).isoformat(),
         "extracts": 0, "facts": 0, "recalls": 0}
        for offset in reversed(range(days))
    ]
    return {
        "counts": {kind: 0 for kind in KINDS},
        "extract": {"runs": 0, "passed": 0, "rejected": 0, "facts": 0, "superseded": 0},
        "recall": {"calls": 0, "results": 0, "staleFiltered": 0, "avgTopK": 0},
        "latency": {kind: {"p50": 0, "p95": 0, "n": 0} for kind in LATENCY_KINDS},
        "daily": daily,
        "lastBrief": None,
    }


def summarize(events: Iterable[dict], days: int = 7, today: date | None = None) -> dict:
    """把事件序列折成一份窗口摘要（global / session 共用同一结构）。

    ``days`` 控制 ``daily`` 的覆盖面与事件窗口；``today`` 可注入，便于测试固定日期。
    """
    days = max(1, int(days))
    today = today or date.today()
    summary = _empty_summary(days, today)
    daily_by_date = {row["date"]: row for row in summary["daily"]}

    latency: dict[str, list[float]] = {kind: [] for kind in LATENCY_KINDS}
    top_ks: list[float] = []
    facts_total = 0

    for event in events:
        kind = event.get("kind")
        if kind in summary["counts"]:
            summary["counts"][kind] += 1

        ms = _num(event.get("ms"))
        if ms is not None and kind in latency:
            latency[kind].append(ms)

        day = _event_date(event)
        row = daily_by_date.get(day.isoformat()) if day is not None else None

        if kind == "extract":
            gate = event.get("gate")
            written = _id_list(event.get("written"))
            if gate == "pass":
                summary["extract"]["passed"] += 1
            elif gate == "reject":
                summary["extract"]["rejected"] += 1
            facts_total += len(written)
            summary["extract"]["superseded"] += _count(event.get("superseded"))
            if row is not None:
                row["extracts"] += 1
                row["facts"] += len(written)
        elif kind == "recall":
            ids = _id_list(event.get("ids"))
            summary["recall"]["calls"] += 1
            summary["recall"]["results"] += len(ids)
            stale = _num(event.get("stale_filtered"))
            if stale is not None:
                summary["recall"]["staleFiltered"] += int(stale)
            top_k = _num(event.get("top_k"))
            if top_k is not None:
                top_ks.append(top_k)
            if row is not None:
                row["recalls"] += 1
        elif kind == "brief":
            summary["lastBrief"] = {
                "chars": int(_num(event.get("chars")) or 0),
                "at": event.get("t"),
            }

    summary["extract"]["runs"] = (
        summary["extract"]["passed"] + summary["extract"]["rejected"]
    ) or summary["counts"]["extract"]
    summary["extract"]["facts"] = facts_total
    if top_ks:
        summary["recall"]["avgTopK"] = round(sum(top_ks) / len(top_ks), 1)
    for kind in LATENCY_KINDS:
        samples = latency[kind]
        summary["latency"][kind] = {
            "p50": _pct(samples, 0.5),
            "p95": _pct(samples, 0.95),
            "n": len(samples),
        }
    return summary


# ---------------------------------------------------------------- vault 体量


def _count_md(directory: Path) -> int:
    """目录下 md 文件数；目录不存在/读失败给 0。"""
    try:
        return sum(1 for path in directory.glob("*.md") if path.is_file())
    except OSError:
        return 0


def _dir_bytes(directory: Path) -> int:
    """目录下所有文件字节数；读失败给 0。"""
    total = 0
    try:
        for path in directory.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def vault_stats(root: Path | str | None = None, facts: dict[str, Any] | None = None) -> dict:
    """统计 vault 当前体量：facts 总数、stale（valid_to 非空）数、视图文件数、journal 字节数。

    journal 走 ``Journal.replay()``（真源），视图走 ``vault_views/`` 目录的 md 计数；
    任一部分读不到都给 0，绝不抛异常。
    """
    vault_root = telemetry.resolve_root(root)
    stats = {"facts": 0, "stale": 0, "entities": 0, "topics": 0, "journalBytes": 0}
    try:
        from liansu.vault.journal import Journal

        loaded = facts if facts is not None else {
            fact.id: fact for fact in Journal(vault_root / JOURNAL_DIR).replay()
        }
        stats["facts"] = len(loaded)
        stats["stale"] = sum(1 for fact in loaded.values() if fact.valid_to is not None)
    except Exception:  # noqa: BLE001 —— 真源缺失/损坏都退化为 0
        pass
    views = vault_root / VIEWS_DIR
    stats["entities"] = _count_md(views / ENTITIES_DIR)
    stats["topics"] = _count_md(views / TOPICS_DIR)
    stats["journalBytes"] = _dir_bytes(vault_root / JOURNAL_DIR)
    return stats


# ---------------------------------------------------------------- 事实正文


def _fact_payload(fact: Any) -> dict[str, Any]:
    """把一条 Fact 摊平成插件可直接渲染的字段（正文放在 ``statement``）。"""
    return {
        "id": getattr(fact, "id", None),
        "subject": getattr(fact, "subject", None),
        "predicate": getattr(fact, "predicate", None),
        "object": getattr(fact, "object", None),
        "statement": getattr(fact, "statement", None),
        "confidence": getattr(fact, "confidence", None),
        "validFrom": _iso_date(getattr(fact, "valid_from", None)),
        "validTo": _iso_date(getattr(fact, "valid_to", None)),
        "topics": list(getattr(fact, "topics", []) or []),
        "source": getattr(fact, "source", None),
    }


def _iso_date(value: Any) -> str | None:
    try:
        return value.isoformat() if value is not None else None
    except AttributeError:
        return str(value)


def load_facts(root: Path | str | None = None) -> dict[str, Any]:
    """journal 重放成 id → Fact；真源缺失/损坏时给空字典（面板降级而不是报错）。"""
    vault_root = telemetry.resolve_root(root)
    try:
        from liansu.vault.journal import Journal

        return {fact.id: fact for fact in Journal(vault_root / JOURNAL_DIR).replay()}
    except Exception:  # noqa: BLE001
        return {}


def _resolve_facts(ids: Any, facts_by_id: dict[str, Any] | None,
                   scores: Any = None, limit: int = DETAIL_FACT_LIMIT) -> dict[str, Any]:
    """把 id 列表解析成事实正文；找不到的 id 保留 ``found: false``，由前端提示。"""
    listed = _id_list(ids)
    by_id = facts_by_id or {}
    score_list = scores if isinstance(scores, (list, tuple)) else []
    items: list[dict[str, Any]] = []
    for index, fid in enumerate(listed[:limit]):
        fact = by_id.get(fid)
        if fact is None:
            item: dict[str, Any] = {"id": fid, "found": False}
        else:
            item = _fact_payload(fact)
            item["found"] = True
        if index < len(score_list) and isinstance(score_list[index], (int, float)):
            item["score"] = round(float(score_list[index]), 4)
        items.append(item)
    return {"items": items, "total": len(listed), "omitted": max(0, len(listed) - len(items))}


# ---------------------------------------------------------------- recent


def _summary_text(event: dict) -> str:
    """单条事件的中文短句摘要（给 DSH 插件的时间线展示）。"""
    kind = event.get("kind")
    if kind == "extract":
        if event.get("gate") == "reject":
            return "gate 拒绝，未写入"
        text = f"写入 {len(_id_list(event.get('written')))} 条"
        superseded = _count(event.get("superseded"))
        if superseded:
            text += f"，取代 {superseded} 条"
        return text
    if kind == "recall":
        text = f"召回 {len(_id_list(event.get('ids')))} 条"
        stale = _num(event.get("stale_filtered"))
        if stale:
            text += f"，过滤过期 {int(stale)} 条"
        return text
    if kind == "expand":
        return f"扩散 {len(_id_list(event.get('ids')))} 条"
    if kind == "brief":
        return f"生成摘要 {int(_num(event.get('chars')) or 0)} 字符"
    if kind == "rebuild":
        return "重建完成" if event.get("ok") else "重建失败"
    return str(kind or "未知事件")


def _detail(event: dict, facts_by_id: dict[str, Any] | None = None) -> dict[str, Any]:
    """按事件类型把「可展开明细」抽出来；缺字段给 None / []，由前端按需渲染。

    保留原始字段名（``topK`` 用驼峰）是为了和插件里的渲染键一一对上；
    id 会额外解析成事实正文（``writtenFacts`` / ``supersededFacts`` / ``facts``），
    这样插件展开后看到的是记忆原文（statement、三元组、有效期、来源），而不只是 id。
    """
    kind = event.get("kind")
    if kind == "extract":
        return {
            "gate": event.get("gate"),
            "chars": _num(event.get("chars")),
            "written": _id_list(event.get("written")),
            "superseded": _id_list(event.get("superseded")),
            "model": event.get("model"),
            "writtenFacts": _resolve_facts(event.get("written"), facts_by_id),
            "supersededFacts": _resolve_facts(event.get("superseded"), facts_by_id),
        }
    if kind == "recall":
        scores = event.get("scores")
        top_k = event.get("top_k")
        return {
            "query": event.get("query"),
            "topK": int(top_k) if isinstance(top_k, (int, float)) and not isinstance(top_k, bool) else None,
            "stage": event.get("stage"),
            "ids": _id_list(event.get("ids")),
            "scores": [round(float(s), 4) for s in scores if isinstance(s, (int, float))]
            if isinstance(scores, (list, tuple)) else [],
            "staleFiltered": _num(event.get("stale_filtered")),
            "facts": _resolve_facts(event.get("ids"), facts_by_id, scores=event.get("scores")),
        }
    if kind == "expand":
        hops = event.get("hops")
        return {
            "factId": event.get("fact_id"),
            "hops": int(hops) if isinstance(hops, (int, float)) and not isinstance(hops, bool) else None,
            "ids": _id_list(event.get("ids")),
            "facts": _resolve_facts(event.get("ids"), facts_by_id),
        }
    if kind == "brief":
        return {"chars": _num(event.get("chars")), "facts": _num(event.get("facts"))}
    if kind == "rebuild":
        return {"ok": event.get("ok")}
    return {}


def _recent_item(event: dict, facts_by_id: dict[str, Any] | None = None) -> dict:
    """把一条原始事件规整成 recent 条目（7 个字段，``detail`` 供前端展开展示）。"""
    kind = event.get("kind")
    source = event.get("written") if kind == "extract" else event.get("ids")
    ms = _num(event.get("ms"))
    if ms is not None:
        ms = int(ms) if ms.is_integer() else round(ms, 1)
    return {
        "t": event.get("t"),
        "kind": kind,
        "session": event.get("session"),
        "summary": _summary_text(event),
        "facts": _id_list(source),
        "ms": ms,
        "detail": _detail(event, facts_by_id),
    }


def recent_events(events: Iterable[dict], session: str | None = None,
                  limit: int = RECENT_LIMIT,
                  facts_by_id: dict[str, Any] | None = None) -> list[dict]:
    """最近 limit 条事件（新→旧）；给 session 时只保留该 session。"""
    picked = [
        event for event in events
        if session is None or event.get("session") == session
    ]
    return [_recent_item(event, facts_by_id) for event in telemetry.tail(picked, limit)]


# ---------------------------------------------------------------- 主入口


def build_metrics(
    root: Path | str | None = None,
    session: str | None = None,
    days: int = 7,
    *,
    now: datetime | None = None,
) -> dict:
    """聚合出 CLI ``liansu metrics --json`` 的完整对象（契约见模块 docstring）。

    root 默认取 ``LIANSU_ROOT`` 或仓库根；``session`` 非空时额外给出 session 摘要
    且 recent 只保留该 session；``now`` 可注入，供测试固定「今天」。
    """
    days = max(1, int(days))
    current = now or datetime.now().astimezone()
    today = current.date()
    vault_root = telemetry.resolve_root(root)

    events = [event for event in telemetry.read_events(vault_root)
              if _in_window(event, today, days)]

    payload: dict[str, Any] = {
        "generatedAt": current.isoformat(timespec="seconds"),
        "windowDays": days,
        "global": summarize(events, days=days, today=today),
        "session": None,
        "vault": None,   # 下面补，复用同一次 journal 重放
        "recent": None,
    }
    facts_by_id = load_facts(vault_root)
    payload["vault"] = vault_stats(vault_root, facts=facts_by_id)
    payload["recent"] = recent_events(events, session=session, facts_by_id=facts_by_id)
    if session:
        scoped = [event for event in events if event.get("session") == session]
        payload["session"] = summarize(scoped, days=days, today=today)
    return payload
