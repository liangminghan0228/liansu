"""T9 · expand 多跳关联（薄封装 GraphIndex.neighbors）。

限制最大 hops 与返回数，结果过时效过滤；无图时优雅降级为空列表。
"""
from __future__ import annotations

from liansu.index.graph import GraphIndex
from liansu.schema import Fact

MAX_HOPS = 3


def expand(
    graph: GraphIndex | None,
    fact_id: str,
    hops: int = 1,
    max_results: int = 10,
    valid_only: bool = True,
) -> list[Fact]:
    if graph is None:
        return []
    hops = max(1, min(hops, MAX_HOPS))
    try:
        related = graph.neighbors(fact_id, hops=hops, valid_only=valid_only)
    except Exception:
        return []           # 图异常不应打断调用方
    return related[: max(1, max_results)]
