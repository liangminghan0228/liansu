#!/usr/bin/env python
"""连溯 demo：一段会话 -> record -> recall -> expand 全链路演示。

用法：
  python demo.py            # 用 MockProvider，离线可跑，演示链路
  python demo.py --real     # 用真实 Windows LLM 服务（需 /health 可达）
  python demo.py --real --embed   # 额外加载 bge 向量模型（首次会下载）
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from liansu.config import load_config
from liansu.extract.pipeline import Pipeline
from liansu.index.bm25 import BM25Index
from liansu.index.expand import expand
from liansu.index.fuse import FuseIndex
from liansu.index.graph import GraphIndex
from liansu.schema import Fact
from liansu.vault.journal import Journal
from liansu.vault.views import ViewBuilder


DIALOG = [
    {"role": "user", "content": "我决定求职目标城市就定北京了，西安作为备选。另外我身高188体重82。"},
    {"role": "assistant", "content": "记下了：目标城市北京（备选西安），188cm/82kg。"},
    {"role": "user", "content": "我之前在理想汽车干了差不多两年，2026年5月离职，现在空窗找工作。"},
    {"role": "assistant", "content": "了解，离职空窗中，目标 Agent 开发岗。"},
]


def demo_facts(source: str) -> list[Fact]:
    """离线演示用的预置事实（模拟 LLM 抽取结果）。"""
    return [
        Fact.from_dict(dict(id="F-D1", subject="梁明晗", predicate="求职目标城市", object="北京",
                            statement="梁明晗求职目标城市定为北京，西安备选", valid_from=date(2026, 11, 22),
                            confidence=0.92, topics=["求职", "城市"], source=source)),
        Fact.from_dict(dict(id="F-D2", subject="梁明晗", predicate="身高体重", object="188cm/82kg",
                            statement="梁明晗身高188cm体重82kg", valid_from=date(2026, 11, 22),
                            confidence=0.95, topics=["个人"], source=source)),
        Fact.from_dict(dict(id="F-D3", subject="梁明晗", predicate="最近任职", object="理想汽车（2026-05 离职）",
                            statement="梁明晗在理想汽车工作至2026年5月，现已离职", valid_from=date(2026, 5, 1),
                            confidence=0.9, topics=["工作", "求职"], source=source)),
    ]


def main() -> int:
    real = "--real" in sys.argv
    want_embed = "--embed" in sys.argv
    cfg = load_config()

    workdir = Path(tempfile.mkdtemp(prefix="liansu-demo-"))
    journal = Journal(workdir / "journal")
    bm25, graph = BM25Index(), GraphIndex()

    if real:
        from liansu.providers.api import ApiProvider
        provider = ApiProvider(config=cfg)
        print(f"[mode] 真实 LLM: {cfg['llm']['base_url']} model={cfg['llm']['model']}")
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "tests" / "fixtures"))
        from mock_provider import MockProvider
        provider = MockProvider(facts=demo_facts("demo://offline"))
        print("[mode] MockProvider（离线演示）")

    pipeline = Pipeline(journal, provider, indexes=[bm25, graph], state_path=workdir / "processed.json")

    print("\n=== 1) record：从会话抽取并落盘 ===")
    written = pipeline.record(DIALOG, source="demo://session-1")
    if real and not written:
        print("（真实模型 gate 判定无需记忆，改用预置事实演示后续流程）")
        for f in demo_facts("demo://real"):
            journal.append(f)
        written = demo_facts("demo://real")
    for f in written:
        print(f"  + {f.id}: {f.statement}  (conf={f.confidence})")

    print("\n=== 2) recall：检索「求职目标城市」 ===")
    fuse = FuseIndex(journal, {"bm25": bm25, "graph": graph}, config=cfg)
    for r in fuse.recall("求职目标城市", top_k=3):
        print(f"  [{r.score:.4f}] {r.fact_id}: {r.statement}")

    print("\n=== 3) expand：从「求职目标城市」做 1 跳关联 ===")
    if written:
        for f in expand(graph, written[0].id, hops=1):
            print(f"  -> {f.id}: {f.statement}")

    print("\n=== 4) 时效：supersede 后旧事实不再召回 ===")
    old = journal.find_by_key("梁明晗", "求职目标城市")
    if old:
        new = Fact.from_dict(dict(old.to_dict(), id="F-D1b", object="西安", valid_from=date.today(),
                                  statement="梁明晗后来把求职目标城市改为西安", supersedes=old.id))
        journal.append(new)
        journal.supersede(old.id, new.id, at=datetime.now(), when=date.today())
        bm25b = BM25Index()
        bm25b.rebuild(journal.replay())          # 重建索引后才能检索到新事实
        hits = [r.fact_id for r in FuseIndex(journal, {"bm25": bm25b}, config=cfg).recall("求职目标城市")]
        print(f"  supersede 后命中: {hits}（应命中新事实、不含旧 {old.id}）")

    print("\n=== 5) 派生视图 ===")
    report = ViewBuilder(workdir / "views", journal).rebuild()
    print(f"  {report}")

    print(f"\n[done] 运行产物: {workdir}")
    if want_embed:
        print("[note] --embed 仅对向量检索生效，本 demo 默认走 BM25+图，避免下载模型")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
