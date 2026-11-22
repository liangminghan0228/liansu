"""T10 MCP server：5 工具注册、impl 逻辑、in-memory Client 调用。"""
import asyncio
from datetime import date
from pathlib import Path

import pytest
from fastmcp import Client

import liansu.mcp_server as ms
from liansu.extract.pipeline import Pipeline
from liansu.index.bm25 import BM25Index
from liansu.index.fuse import FuseIndex
from liansu.index.graph import GraphIndex
from liansu.schema import Fact
from liansu.vault.journal import Journal
from liansu.vault.views import ViewBuilder

from tests.fixtures.mock_index import MockIndex
from tests.fixtures.mock_provider import MockProvider


def make_fact(fid, obj="北京", **kw):
    base = dict(id=fid, subject="梁明晗", predicate="求职目标城市", object=obj,
                statement=f"梁明晗求职目标城市{obj}", valid_from=date(2026, 11, 21),
                confidence=0.9, topics=["求职"], source="test://")
    base.update(kw)
    return Fact.from_dict(base)


@pytest.fixture
def ctx(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    bm25, graph, vector = BM25Index(), GraphIndex(), MockIndex()
    indexes = {"bm25": bm25, "vector": vector, "graph": graph}
    provider = MockProvider(facts=[make_fact("F-1"), make_fact("F-2", obj="西安", predicate="备选城市")])
    pipeline = Pipeline(journal, provider, indexes=[bm25, graph], state_path=tmp_path / "p.json")
    return {
        "journal": journal, "pipeline": pipeline, "indexes": indexes,
        "fuse": FuseIndex(journal, indexes), "graph": graph,
        "views": ViewBuilder(tmp_path / "vault_views", journal),
        "root": tmp_path, "top_k": 5, "config": {},
    }


def test_five_tools_registered():
    async def go():
        async with Client(ms.mcp) as c:
            tools = await c.list_tools()
            return sorted(t.name for t in tools)
    names = asyncio.run(go())
    assert names == ["brief", "expand", "rebuild", "recall", "record"]


def test_record_impl(ctx):
    r = ms.record_impl(ctx, '[{"role":"user","content":"我决定求职目标城市定北京，备选西安。"}]', source="s1")
    assert r["ok"] and r["count"] == 2
    assert ctx["journal"].get_by_id("F-1") is not None


def test_recall_impl(ctx):
    ms.record_impl(ctx, '[{"role":"user","content":"我决定求职目标城市定北京，备选西安。"}]', source="s1")
    r = ms.recall_impl(ctx, "求职目标城市", top_k=5)
    assert r["ok"] and r["count"] >= 1


def test_expand_impl(ctx):
    ms.record_impl(ctx, '[{"role":"user","content":"我决定求职目标城市定北京，备选西安。"}]', source="s1")
    r = ms.expand_impl_tool(ctx, "F-1", hops=1)
    assert r["ok"] and isinstance(r["facts"], list)


def test_brief_impl(ctx):
    ms.record_impl(ctx, '[{"role":"user","content":"我决定求职目标城市定北京，备选西安。"}]', source="s1")
    r = ms.brief_impl(ctx, max_tokens=500)
    assert r["ok"] and "brief" in r


@pytest.mark.skipif(not Path("src/liansu/rebuild.py").exists(), reason="T12 rebuild 尚未落地")
def test_rebuild_impl(ctx):
    r = ms.rebuild_impl(ctx, verify=False)
    assert r["ok"]


def test_call_tool_via_client(ctx, monkeypatch):
    monkeypatch.setattr(ms, "_CTX", ctx)

    async def go():
        async with Client(ms.mcp) as c:
            await c.call_tool("record", {"dialog": '[{"role":"user","content":"我决定求职目标城市定北京。"}]', "source": "s1"})
            res = await c.call_tool("recall", {"query": "求职目标城市", "top_k": 3})
            return res.data
    data = asyncio.run(go())
    assert data["ok"] is True
    assert data["count"] >= 1
