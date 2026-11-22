"""BM25 索引 — rank_bm25 + jieba 中文分词（T6）。

rank_bm25 默认按空格切词，中文必须先由 jieba 分词，否则整句变成一个 token。
索引是 journal 的派生品，可随时用 rebuild() 全量重建。
"""
from __future__ import annotations

import jieba
from rank_bm25 import BM25Okapi

from liansu.index.base import Index
from liansu.schema import Fact

# 专有名词/人名注册进 jieba 词典，保证整词切分、精确查询可命中。
for _word in ("梁明晗", "求职目标城市"):
    jieba.add_word(_word)


def _tokenize(text: str) -> list[str]:
    """jieba 分词并丢弃空白 token。"""
    return [t for t in jieba.lcut(text) if t.strip()]


class BM25Index(Index):
    """基于 BM25Okapi 的增量索引：维护 fact_id -> tokens，add 后重建打分器。"""

    def __init__(self) -> None:
        self._fact_ids: list[str] = []
        self._tokens: dict[str, list[str]] = {}
        self._bm25: BM25Okapi | None = None

    def _refresh(self) -> None:
        """用当前 tokens 重建 BM25 打分器；空语料时置空。"""
        if self._fact_ids:
            self._bm25 = BM25Okapi([self._tokens[fid] for fid in self._fact_ids])
        else:
            self._bm25 = None

    def add(self, facts: list[Fact]) -> None:
        for f in facts:
            if f.id not in self._tokens:
                self._fact_ids.append(f.id)
            self._tokens[f.id] = _tokenize(f.statement_for_index())
        self._refresh()

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        if self._bm25 is None or not query or not query.strip():
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        scores = self._bm25.get_scores(q_tokens)
        # BM25Okapi 的 idf 无 +1 平滑，语料小时相关文档也可能得 0 分甚至负分；
        # 因此用「是否有词项重叠」判定命中，再用 BM25 分数排序（score 越大越相关）。
        q_set = set(q_tokens)
        ranked = [
            (fid, float(scores[i]))
            for i, fid in enumerate(self._fact_ids)
            if q_set & set(self._tokens[fid])
        ]
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked[:top_k]

    def rebuild(self, facts: list[Fact]) -> None:
        """清空后按全量 fact 重建。"""
        self._fact_ids = []
        self._tokens = {}
        self._bm25 = None
        self.add(facts)

    def close(self) -> None:
        """清理索引状态。"""
        self._fact_ids = []
        self._tokens = {}
        self._bm25 = None
