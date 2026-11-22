"""Mock index — 并行期索引替身，按 statement 关键词做简单匹配。"""
from __future__ import annotations

from liansu.index.base import Index
from liansu.schema import Fact


class MockIndex(Index):
    def __init__(self):
        self.facts: dict[str, Fact] = {}

    def add(self, facts: list[Fact]) -> None:
        for f in facts:
            self.facts[f.id] = f

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        scored = []
        for fid, f in self.facts.items():
            hits = sum(1 for tok in query if tok in f.statement_for_index())
            if hits:
                scored.append((fid, float(hits)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def rebuild(self, facts: list[Fact]) -> None:
        self.facts = {f.id: f for f in facts}

    def close(self) -> None:
        self.facts.clear()
