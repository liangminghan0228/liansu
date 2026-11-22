"""双时态图索引 — networkx 自建实现（T7 / T9 基础）。

TECH_DESIGN §5.6 方案 B：不引入 neo4j / Graphiti，用 networkx.MultiDiGraph
在进程内建图，零外部服务依赖。对外接口（add / search / rebuild / close +
neighbors / find_fact）与 Graphiti 版保持一致，后续可无痛替换实现。

图结构：
- fact 节点：node id = fact.id，属性 kind="fact"，携带双时态
  （valid_from / valid_to 为事实有效时间，transaction_time 为入库时间）；
- 实体节点：node id = "E:" + 实体名，属性 kind="entity"，实体来源为
  subject / object / topics；
- 边：fact -> 实体。边同样带 valid_from / valid_to / transaction_time，
  supersede 时旧边不删除，只由 journal 层把 valid_to 写进 fact 后重新 add。

图是 journal 的派生品，可随时 rebuild() 全量重建。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import networkx as nx

from liansu.index.base import Index
from liansu.schema import Fact

ENTITY_PREFIX = "E:"
MAX_HOPS = 3


def _entity_id(name: str) -> str:
    return f"{ENTITY_PREFIX}{name}"


def _is_expired(fact: Fact, when: date) -> bool:
    """已失效：valid_to 非空且已到/过期（左闭右开，当天即失效）。"""
    return fact.valid_to is not None and fact.valid_to <= when


class GraphIndex(Index):
    """双时态事实图：fact 与实体交替连边，支持多跳关联查询。"""

    def __init__(self) -> None:
        self.graph = nx.MultiDiGraph()
        self._facts: dict[str, Fact] = {}

    # ---------------- 写 ----------------

    def add(self, facts: list[Fact]) -> None:
        """写入 fact / 实体节点与边；同 id 重复 add 覆盖为最新状态（幂等）。"""
        for f in facts:
            self._facts[f.id] = f
            self._upsert_fact_node(f)
            self._upsert_edges(f)

    def _upsert_fact_node(self, f: Fact) -> None:
        self.graph.add_node(
            f.id,
            kind="fact",
            statement=f.statement,
            subject=f.subject,
            predicate=f.predicate,
            object=f.object,
            topics=list(f.topics),
            valid_from=f.valid_from,
            valid_to=f.valid_to,
            transaction_time=datetime.now(timezone.utc),
        )

    def _upsert_edges(self, f: Fact) -> None:
        attrs = {
            "valid_from": f.valid_from,
            "valid_to": f.valid_to,
            "transaction_time": datetime.now(timezone.utc),
        }
        # 先清掉旧出边，避免 fact 更新后残留过期实体连接
        if self.graph.has_node(f.id):
            self.graph.remove_edges_from(list(self.graph.out_edges(f.id, keys=True)))

        targets: list[tuple[str, str]] = [(f.subject, "subject"), (f.object, "object")]
        targets += [(t, f"topic:{t}") for t in f.topics]
        for name, rel in targets:
            if not name:
                continue
            ent = _entity_id(name)
            self.graph.add_node(ent, kind="entity", name=name)
            self.graph.add_edge(f.id, ent, key=rel, rel=rel.split(":", 1)[0], **attrs)

    # ---------------- 读 ----------------

    def find_fact(self, fact_id: str) -> Fact | None:
        """按 id 取 Fact 对象（neighbors / expand 需要返回 Fact）。"""
        return self._facts.get(fact_id)

    def neighbors(self, fact_id: str, hops: int = 1, valid_only: bool = True) -> list[Fact]:
        """从 fact 出发 BFS，沿 fact→实体→fact 交替路径返回多跳关联 fact。

        hops 钳制在 [1, MAX_HOPS]；valid_only=True 时剔除当前已失效的 fact。
        """
        if fact_id not in self._facts or not self.graph.has_node(fact_id):
            return []
        hops = max(1, min(int(hops), MAX_HOPS))
        today = date.today()

        seen_facts: set[str] = {fact_id}
        frontier: list[str] = [fact_id]
        found: list[str] = []

        for _ in range(hops):
            next_frontier: list[str] = []
            for fid in frontier:
                for ent in self.graph.successors(fid):
                    if self.graph.nodes[ent].get("kind") != "entity":
                        continue
                    for src in self.graph.predecessors(ent):
                        if self.graph.nodes[src].get("kind") != "fact" or src in seen_facts:
                            continue
                        seen_facts.add(src)
                        next_frontier.append(src)
            next_frontier.sort()
            found.extend(next_frontier)
            if not next_frontier:
                break
            frontier = next_frontier

        result: list[Fact] = []
        for fid in found:
            fact = self._facts.get(fid)
            if fact is None:
                continue
            if valid_only and _is_expired(fact, today):
                continue
            result.append(fact)
        return result

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """兜底关键词检索：在 statement/subject/object/topics 里数命中词数。"""
        if not query or not query.strip():
            return []
        tokens = [t.lower() for t in query.split() if t.strip()]
        if not tokens:
            return []

        scored: list[tuple[str, float]] = []
        for fid, fact in self._facts.items():
            haystack = " ".join(
                [fact.statement, fact.subject, fact.object, *fact.topics]
            ).lower()
            hits = sum(1 for tok in tokens if tok in haystack)
            if hits:
                scored.append((fid, float(hits)))

        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[: max(0, top_k)]

    # ---------------- 生命周期 ----------------

    def rebuild(self, facts: list[Fact]) -> None:
        """清空图后按全量 fact 重建。"""
        self.close()
        self.add(facts)

    def close(self) -> None:
        self.graph.clear()
        self._facts.clear()

    # ---------------- 调试 ----------------

    def node_count(self) -> int:
        return self.graph.number_of_nodes()

    def edge_count(self) -> int:
        return self.graph.number_of_edges()
