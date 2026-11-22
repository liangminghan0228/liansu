"""T6 — BM25Index 单元测试。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from liansu.index.bm25 import BM25Index
from liansu.schema import Fact

FIXTURE = Path(__file__).parent / "fixtures" / "fact.yaml"


def _fixture_fact() -> Fact:
    return Fact.from_yaml(FIXTURE.read_text(encoding="utf-8"))


def _fact(fid: str, subject: str, obj: str, statement: str, topics: list[str]) -> Fact:
    return Fact(
        id=fid,
        subject=subject,
        predicate="求职目标城市",
        object=obj,
        statement=statement,
        valid_from=date(2026, 11, 21),
        confidence=0.9,
        topics=topics,
    )


def test_empty_index_search_returns_empty():
    assert BM25Index().search("北京") == []


def test_empty_query_returns_empty():
    idx = BM25Index()
    idx.add([_fixture_fact()])
    assert idx.search("") == []
    assert idx.search("   ") == []


def test_exact_proper_noun_query_hits():
    """人名/专名整词查询必须命中（jieba 切分不切碎）。"""
    idx = BM25Index()
    fact = _fixture_fact()
    idx.add([fact])

    for query in ("梁明晗", "求职目标城市", "北京"):
        results = idx.search(query)
        assert results, f"查询 {query!r} 未命中"
        assert results[0][0] == fact.id


def test_incremental_add_and_top_k():
    idx = BM25Index()
    f1 = _fixture_fact()
    f2 = _fact("F-2", "张三", "上海", "张三决定求职目标城市定为上海", ["求职", "城市"])

    idx.add([f1])
    assert [fid for fid, _ in idx.search("北京")] == [f1.id]
    assert idx.search("上海") == []

    idx.add([f2])
    assert [fid for fid, _ in idx.search("上海")] == [f2.id]
    assert len(idx.search("求职", top_k=1)) == 1
    assert len(idx.search("求职", top_k=2)) == 2


def test_relevance_ordering():
    idx = BM25Index()
    f1 = _fixture_fact()
    f2 = _fact("F-2", "张三", "上海", "张三决定求职目标城市定为上海", ["求职", "城市"])
    idx.add([f1, f2])

    results = idx.search("梁明晗 北京")
    assert results[0][0] == f1.id
    if len(results) > 1:
        assert results[0][1] >= results[1][1]


def test_rebuild_is_consistent_and_clears():
    idx = BM25Index()
    facts = [
        _fixture_fact(),
        _fact("F-2", "张三", "上海", "张三决定求职目标城市定为上海", ["求职", "城市"]),
    ]
    idx.add(facts)
    before = idx.search("求职", top_k=5)

    idx.rebuild(facts)
    assert idx.search("求职", top_k=5) == before

    idx.rebuild([])
    assert idx.search("北京") == []

    # 重复 add 同一 id 不应产生重复结果
    idx.rebuild([_fixture_fact()])
    idx.add([_fixture_fact()])
    assert [fid for fid, _ in idx.search("梁明晗")] == [_fixture_fact().id]


def test_close_clears_index():
    idx = BM25Index()
    idx.add([_fixture_fact()])
    assert idx.search("北京")

    idx.close()
    assert idx.search("北京") == []
