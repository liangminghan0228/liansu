"""T8 检索融合：RRF 排序 / 时效过滤 / Stage 开关 / 边界。"""
from datetime import date, datetime
from pathlib import Path

import pytest

from liansu.index.base import Index
from liansu.index.fuse import FuseIndex, ScoredFact
from liansu.schema import Fact
from liansu.vault.journal import Journal


class StubIndex(Index):
    def __init__(self, results):
        self.results = results

    def add(self, facts):
        pass

    def search(self, query, top_k=5):
        return self.results[:top_k]

    def rebuild(self, facts):
        pass

    def close(self):
        pass


def make_fact(fid, statement=None, **kw):
    base = dict(id=fid, subject="梁明晗", predicate=f"属性{fid}", object="x",
                statement=statement or f"事实{fid}", valid_from=date(2026, 1, 1),
                confidence=0.9, topics=["t"], source="test://")
    base.update(kw)
    return Fact.from_dict(base)


@pytest.fixture
def journal(tmp_path: Path):
    j = Journal(tmp_path / "journal")
    j.append(make_fact("A", "梁明晗求职目标城市北京"))
    j.append(make_fact("B", "梁明晗身高188"))
    j.append(make_fact("C", "梁明晗技术栈RAG"))
    j.append(make_fact("OLD", "梁明晗在理想汽车工作", predicate="任职公司"))
    j.append(make_fact("NEW", "梁明晗已离职", predicate="任职状态"))
    j.supersede("OLD", "NEW", at=datetime(2026, 6, 1))
    return j


def test_rrf_ranking(journal):
    bm25 = StubIndex([("A", 10.0), ("B", 5.0)])
    vector = StubIndex([("B", 0.9), ("C", 0.8)])
    fuse = FuseIndex(journal, {"bm25": bm25, "vector": vector}, k=60,
                     weights={"bm25": 1.0, "vector": 1.0})
    res = fuse.recall("求职", top_k=3, stage=1)
    assert [r.fact_id for r in res][0] == "B"      # 两路都命中，RRF 最高
    assert set(r.fact_id for r in res) == {"A", "B", "C"}


def test_stage0_bm25_only(journal):
    bm25 = StubIndex([("A", 1.0)])
    vector = StubIndex([("B", 1.0)])
    fuse = FuseIndex(journal, {"bm25": bm25, "vector": vector})
    res = fuse.recall("q", stage=0)
    assert [r.fact_id for r in res] == ["A"]


def test_temporal_filter(journal):
    # OLD 已 superseded：stage3 剔除；stage2 保留（消融用）
    bm25 = StubIndex([("OLD", 9.0), ("NEW", 8.0)])
    fuse = FuseIndex(journal, {"bm25": bm25})
    ids3 = [r.fact_id for r in fuse.recall("任职", stage=3)]
    assert "OLD" not in ids3
    ids2 = [r.fact_id for r in fuse.recall("任职", stage=2)]
    assert "OLD" in ids2


def test_empty_and_no_hit(journal):
    fuse = FuseIndex(journal, {"bm25": StubIndex([])})
    assert fuse.recall("") == []
    assert fuse.recall("无关", stage=3) == []


def test_scored_fact_fields(journal):
    fuse = FuseIndex(journal, {"bm25": StubIndex([("A", 1.0)])})
    r = fuse.recall("求职")[0]
    assert isinstance(r, ScoredFact)
    d = r.to_dict()
    for key in ("fact_id", "statement", "confidence", "valid_from", "valid_to", "source", "score"):
        assert key in d


def test_missing_index_graceful(journal):
    fuse = FuseIndex(journal, {})          # 无任何索引
    assert fuse.recall("求职", stage=3) == []
