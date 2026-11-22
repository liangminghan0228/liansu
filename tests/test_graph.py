"""T7 / T9 — GraphIndex（networkx 双时态图）单元测试。"""
from __future__ import annotations

from datetime import date

from liansu.index.graph import GraphIndex
from liansu.schema import Fact

D = date(2026, 11, 21)


def _fact(
    fid: str,
    subject: str,
    obj: str,
    statement: str = "",
    topics: list[str] | None = None,
    predicate: str = "求职目标城市",
    valid_to: date | None = None,
) -> Fact:
    return Fact(
        id=fid,
        subject=subject,
        predicate=predicate,
        object=obj,
        statement=statement or f"{subject}{predicate}是{obj}",
        valid_from=D,
        valid_to=valid_to,
        confidence=0.9,
        topics=topics or [],
    )


def _three_facts() -> list[Fact]:
    """f1 -(梁明晗)- f2 -(上海)- f3：f1 与 f3 无共享实体，必须两跳。"""
    return [
        _fact("F1", "梁明晗", "北京", "梁明晗求职目标城市是北京", ["求职", "城市"]),
        _fact("F2", "梁明晗", "上海", "梁明晗居住城市是上海", ["城市"], predicate="居住城市"),
        _fact("F3", "上海", "某某科技", "上海有某某科技", ["公司"], predicate="所在地公司"),
    ]


def test_neighbors_via_shared_entity():
    idx = GraphIndex()
    idx.add(_three_facts())

    got = idx.neighbors("F1", hops=1)
    assert [f.id for f in got] == ["F2"]
    assert all(isinstance(f, Fact) for f in got)


def test_neighbors_two_hops():
    idx = GraphIndex()
    idx.add(_three_facts())

    one_hop = {f.id for f in idx.neighbors("F1", hops=1)}
    two_hop = {f.id for f in idx.neighbors("F1", hops=2)}
    assert one_hop == {"F2"}
    assert two_hop == {"F2", "F3"}


def test_hops_are_clamped_to_three():
    idx = GraphIndex()
    # 链式结构：结0 - 结1 - 结2 - 结3 - 结4 - 结5
    chain = [
        _fact(f"C{i}", f"结{i}", f"结{i + 1}", f"结{i}连着结{i + 1}", predicate="相邻")
        for i in range(5)
    ]
    idx.add(chain)

    far = {f.id for f in idx.neighbors("C0", hops=99)}
    assert far == {"C1", "C2", "C3"}, "hops 超过 3 必须钳制，不能走到 C4"

    # hops<1 也钳制为 1
    assert {f.id for f in idx.neighbors("C0", hops=0)} == {"C1"}


def test_valid_only_filters_expired_fact():
    idx = GraphIndex()
    live = _fact("LIVE", "梁明晗", "北京")
    dead = _fact("DEAD", "梁明晗", "杭州", valid_to=D, predicate="旧目标城市")
    idx.add([live, dead])

    assert idx.find_fact("DEAD") is not None, "过期 fact 仍应留在图中"
    assert {f.id for f in idx.neighbors("LIVE", hops=1)} == set()
    assert {f.id for f in idx.neighbors("LIVE", hops=1, valid_only=False)} == {"DEAD"}


def test_supersede_keeps_old_node_but_filters_it():
    idx = GraphIndex()
    old = _fact("OLD", "梁明晗", "北京", valid_to=D)
    new = _fact("NEW", "梁明晗", "上海", "梁明晗求职目标城市是上海")
    idx.add([old])
    idx.add([new])

    assert idx.graph.has_node("OLD"), "supersede 后旧 fact 节点不应被删除"
    assert {f.id for f in idx.neighbors("NEW", hops=1)} == set()
    assert {f.id for f in idx.neighbors("NEW", hops=1, valid_only=False)} == {"OLD"}


def test_rebuild_is_consistent():
    idx = GraphIndex()
    facts = _three_facts()
    idx.add(facts)
    before_search = idx.search("城市", top_k=5)
    before_neighbors = [f.id for f in idx.neighbors("F1", hops=2)]

    idx.rebuild(facts)
    assert idx.search("城市", top_k=5) == before_search
    assert [f.id for f in idx.neighbors("F1", hops=2)] == before_neighbors

    idx.rebuild([])
    assert idx.search("城市") == []
    assert idx.neighbors("F1") == []
    assert idx.node_count() == 0


def test_empty_graph_neighbors():
    idx = GraphIndex()
    assert idx.neighbors("F1") == []
    assert idx.find_fact("F1") is None

    idx.add(_three_facts())
    assert idx.neighbors("NOT_EXIST") == []


def test_search_hits_and_empty_query():
    idx = GraphIndex()
    idx.add(_three_facts())

    assert [fid for fid, _ in idx.search("北京")] == ["F1"]
    assert idx.search("") == []
    assert idx.search("   ") == []
    assert idx.search("完全不存在的词") == []

    hits = idx.search("城市", top_k=1)
    assert len(hits) == 1
    assert hits[0][1] > 0
    all_hits = idx.search("城市", top_k=5)
    assert {fid for fid, _ in all_hits} == {"F1", "F2"}
    assert all_hits[0][1] >= all_hits[-1][1]


def test_add_is_idempotent():
    idx = GraphIndex()
    f = _fact("F1", "梁明晗", "北京", "梁明晗求职目标城市是北京", ["求职"])
    idx.add([f])
    nodes, edges = idx.node_count(), idx.edge_count()

    idx.add([f])
    assert (idx.node_count(), idx.edge_count()) == (nodes, edges)
    assert [fid for fid, _ in idx.search("北京")] == ["F1"]


def test_edges_carry_bitemporal_attrs():
    idx = GraphIndex()
    idx.add([_fact("F1", "梁明晗", "北京", topics=["求职"])])

    data = idx.graph.get_edge_data("F1", "E:梁明晗")
    assert data is not None
    edge = next(iter(data.values()))
    assert edge["valid_from"] == D
    assert edge["valid_to"] is None
    assert edge["transaction_time"] is not None

    # subject / object / 每个 topic 各一条边
    assert idx.graph.number_of_edges() == 3


def test_close_clears_graph():
    idx = GraphIndex()
    idx.add(_three_facts())
    assert idx.neighbors("F1")

    idx.close()
    assert idx.neighbors("F1") == []
    assert idx.search("北京") == []
    assert idx.node_count() == 0
