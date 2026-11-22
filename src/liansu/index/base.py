"""索引抽象 — 向量 / BM25 / 图统一契约，全部可从 journal 重建。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from liansu.schema import Fact


class Index(ABC):
    """派生索引接口：add/search/rebuild/close。索引是可丢弃的派生品。"""

    @abstractmethod
    def add(self, facts: list[Fact]) -> None:
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """返回 (fact_id, score)，score 越大越相关。"""
        raise NotImplementedError

    @abstractmethod
    def rebuild(self, facts: list[Fact]) -> None:
        """从全量 fact 重建索引（先清空再写）。"""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError
