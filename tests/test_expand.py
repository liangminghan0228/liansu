"""T9 expand：多跳关联、hops 钳制、结果上限、无图降级、时效过滤。"""
from datetime import date, datetime
from pathlib import Path

import pytest

from liansu.index.expand import MAX_HOPS, expand
from liansu.index.graph import GraphIndex
from liansu.schema import Fact


def make_fact(fid, subject="梁明晗", predicate=None, obj="x", topics=None, **kw):
    base = dict(id=fid, subject=subject, predicate=predicate or f"p{fid}", object=obj,
                statement=f"事实{fid}", valid_from=date(2026, 1, 1), confidence=0.9,
                topics=topics or ["t"], source="test://")
    base.update(kw)
    return Fact.from_dict(base)


@pytest.fixture
def graph():
    g = GraphIndex()
    # A、B 共享实体「北京」；C 与 B 共享实体「RAG」-> A 两跳可达 C
    g.add([
        make_fact("A", subject="梁明晗", predicate="目标城市", obj="北京"),
        make_fact("B", subject="梁明晗", predicate="常驻城市", obj="北京", topics=["RAG"]),
        make_fact("C", subject="梁明晗", predicate="技术栈", obj="RAG"),
    ])
    return g


def test_expand_one_hop(graph):
    res = expand(graph, "A", hops=1)
    assert {f.id for f in res} >= {"B"}          # 通过共享实体「北京」找到 B
    assert "A" not in {f.id for f in res}


def test_expand_two_hops(graph):
    ids = {f.id for f in expand(graph, "A", hops=2)}
    assert "C" in ids                             # 两跳经共享实体到达 C


def test_hops_clamped(graph):
    # 超大 hops 被钳制到 MAX_HOPS，不报错
    res = expand(graph, "A", hops=99)
    assert isinstance(res, list)


def test_max_results(graph):
    res = expand(graph, "A", hops=2, max_results=1)
    assert len(res) <= 1


def test_none_graph_graceful():
    assert expand(None, "A") == []


def test_missing_fact(graph):
    assert expand(graph, "NOT-EXIST") == []


def test_valid_only_filters(tmp_path: Path):
    g = GraphIndex()
    g.add([
        make_fact("A", predicate="目标城市", obj="北京"),
        make_fact("B", predicate="常驻城市", obj="北京"),
    ])
    # 把 B 标为过期后，valid_only=True 应剔除 B
    from liansu.index.graph import GraphIndex as GI  # noqa
    b_expired = make_fact("B", predicate="常驻城市", obj="北京",
                          valid_from=date(2025, 1, 1), valid_to=date(2025, 6, 1))
    g.rebuild([
        make_fact("A", predicate="目标城市", obj="北京"),
        b_expired,
    ])
    ids = {f.id for f in expand(g, "A", hops=1, valid_only=True)}
    assert "B" not in ids
