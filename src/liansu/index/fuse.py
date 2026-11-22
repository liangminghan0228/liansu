"""T8 · 检索融合（读路径核心）。

recall(query, top_k)：
  BM25 + 向量两路召回 -> 加权 RRF 融合 -> 时效过滤 -> TopK 带元数据返回
Stage 开关（供 T15 消融）：
  0 仅 BM25 | 1 +向量 | 2 +图 | 3 +时效（默认）
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from liansu.config import load_config
from liansu.index.base import Index
from liansu.schema import Fact
from liansu.vault.journal import Journal


@dataclass
class ScoredFact:
    fact_id: str
    statement: str
    confidence: float
    valid_from: str
    valid_to: str | None
    source: str
    score: float
    routes: dict[str, int] = field(default_factory=dict)   # 各路上排名，供诊断

    def to_dict(self) -> dict:
        return asdict(self)


class FuseIndex:
    def __init__(
        self,
        journal: Journal,
        indexes: dict[str, Index],
        k: int | None = None,
        weights: dict[str, float] | None = None,
        config: dict | None = None,
    ):
        cfg = config if config is not None else load_config()
        rrf = cfg.get("rrf", {})
        self.journal = journal
        self.indexes = indexes
        self.k = k if k is not None else rrf.get("k", 60)
        self.weights = weights if weights is not None else rrf.get("weights", {})

    def _route(self, name: str, query: str, pool: int) -> list[tuple[str, float]]:
        idx = self.indexes.get(name)
        if idx is None:
            return []
        try:
            return idx.search(query, top_k=pool)
        except NotImplementedError:
            return []

    def recall(
        self,
        query: str,
        top_k: int = 5,
        stage: int = 3,
        when: date | None = None,
    ) -> list[ScoredFact]:
        when = when or date.today()
        if not query.strip():
            return []

        pool = max(top_k * 4, 20)
        routes: list[str] = ["bm25"]
        if stage >= 1:
            routes.append("vector")
        if stage >= 2:
            routes.append("graph")

        fused: dict[str, float] = {}
        route_ranks: dict[str, dict[str, int]] = {}
        for name in routes:
            hits = self._route(name, query, pool)
            w = float(self.weights.get(name, 1.0))
            route_ranks[name] = {}
            for rank, (fid, _score) in enumerate(hits, start=1):
                fused[fid] = fused.get(fid, 0.0) + w / (self.k + rank)
                route_ranks[name][fid] = rank

        if not fused:
            return []

        # 元数据映射（一次重放）
        facts = {f.id: f for f in self.journal.replay()}

        results: list[ScoredFact] = []
        for fid, score in fused.items():
            fact = facts.get(fid)
            if fact is None:
                continue
            if stage >= 3 and not fact.is_valid_at(when):
                continue  # 时效过滤：过期事实不进入结果
            results.append(
                ScoredFact(
                    fact_id=fact.id,
                    statement=fact.statement,
                    confidence=fact.confidence,
                    valid_from=fact.valid_from.isoformat(),
                    valid_to=fact.valid_to.isoformat() if fact.valid_to else None,
                    source=fact.source,
                    score=round(score, 6),
                    routes={n: r[fid] for n, r in route_ranks.items() if fid in r},
                )
            )

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]
