"""T15 · 评测指标 — 纯函数、无 IO，便于单测与复用。

所有函数只依赖「fact_id 序列」和「fact_id 集合」，不关心检索结果的具体类型，
输入输出均为基础类型：results: list[str]，gold / must_not_include: Iterable[str]。
单条 query 返回 0.0~1.0，跨 query 的平均由 runner 负责。
"""
from __future__ import annotations

from typing import Iterable, Sequence

__all__ = ["hit_rate", "recall_at_k", "mrr", "temporal_accuracy"]


def hit_rate(results: Sequence[str], gold_ids: Iterable[str]) -> float:
    """TopK 是否命中任一 gold：命中 1.0，否则 0.0。

    gold 为空视为「无参考答案」，返回 0.0，避免把无标注样本算成满分。
    """
    gold = set(gold_ids)
    if not gold:
        return 0.0
    return 1.0 if gold & set(results) else 0.0


def recall_at_k(results: Sequence[str], gold_ids: Iterable[str], k: int) -> float:
    """前 k 条结果覆盖了多少比例的 gold。

    recall = |top_k ∩ gold| / |gold|；gold 为空或 k<=0 时返回 0.0。
    """
    gold = set(gold_ids)
    if not gold or k <= 0:
        return 0.0
    top = set(results[:k])
    return len(gold & top) / len(gold)


def mrr(results: Sequence[str], gold_ids: Iterable[str]) -> float:
    """第一个命中 gold 的倒数排名（1/rank）；未命中或 gold 为空返回 0.0。"""
    gold = set(gold_ids)
    if not gold:
        return 0.0
    for rank, fact_id in enumerate(results, start=1):
        if fact_id in gold:
            return 1.0 / rank
    return 0.0


def temporal_accuracy(results: Sequence[str], must_not_include: Iterable[str]) -> float:
    """时效正确率：结果中不得出现已被 supersede 的旧 fact_id。

    must_not_include 为空表示该 query 没有「旧事实陷阱」，返回 1.0（真空成立）；
    runner 聚合时只统计有 must_not_include 的 query，避免被无陷阱样本稀释。
    """
    banned = set(must_not_include)
    if not banned:
        return 1.0
    return 0.0 if banned & set(results) else 1.0
