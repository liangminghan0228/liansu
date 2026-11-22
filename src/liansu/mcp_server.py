"""T10 · MCP server — 连溯唯一对外接口，暴露 5 个工具。

工具描述写明使用场景，让 agent 在需要时主动调用：
- record：会话结束/用户明确要求记住时，写入一段对话
- recall：涉及用户画像、历史决策、偏好时，主检索
- expand：需要关联推理/多跳时，从一条事实扩散
- brief：长任务开局要全局画像时（可选，别滥用）
- rebuild：索引/视图损坏或首次搭建时，从 journal 全量重建
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from liansu.config import load_config
from liansu.extract.pipeline import Pipeline
from liansu.index.bm25 import BM25Index
from liansu.index.expand import expand as expand_impl
from liansu.index.fuse import FuseIndex
from liansu.index.graph import GraphIndex
from liansu.index.vector import VectorIndex
from liansu.providers.api import ApiProvider
from liansu.providers.embedder import SentenceTransformerEmbedder
from liansu.vault.journal import Journal
from liansu.vault.views import ViewBuilder

mcp = FastMCP("liansu")

_CTX: dict[str, Any] | None = None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_context(config: dict | None = None, root: Path | None = None) -> dict[str, Any]:
    """组装全部组件；embedder 延迟加载，不在构造时拉模型。"""
    cfg = config or load_config()
    root = root or _project_root()
    vault_cfg = cfg.get("vault", {})

    journal = Journal(root / vault_cfg.get("journal_dir", "journal"))
    embedder = SentenceTransformerEmbedder(cfg.get("embedder", {}).get("model", "BAAI/bge-small-zh-v1.5"))
    vector = VectorIndex(root / ".liansu" / "index.db", embedder)
    bm25 = BM25Index()
    graph = GraphIndex()

    indexes = {"bm25": bm25, "vector": vector, "graph": graph}
    provider = ApiProvider(config=cfg)
    pipeline = Pipeline(
        journal, provider, indexes=[bm25, vector, graph],
        confidence_threshold=cfg.get("confidence_threshold", 0.7),
        state_path=root / ".liansu" / "processed.json",
    )
    fuse = FuseIndex(journal, indexes, config=cfg)
    views = ViewBuilder(root / "vault_views", journal)

    # 冷启动：从 journal 灌一次索引（幂等）
    facts = journal.replay()
    if facts:
        for name in ("bm25", "graph"):
            indexes[name].rebuild(facts)
        # vector 按需懒加载，避免启动即拉模型

    return {
        "config": cfg, "root": root, "journal": journal, "pipeline": pipeline,
        "indexes": indexes, "fuse": fuse, "graph": graph, "views": views,
        "top_k": cfg.get("top_k", 5),
    }


def get_context() -> dict[str, Any]:
    global _CTX
    if _CTX is None:
        _CTX = build_context()
    return _CTX


def _parse_dialog(dialog: str) -> list[dict[str, str]]:
    """接受 JSON（turns 列表或 {"turns": [...]}）或纯文本（单轮 user）。"""
    try:
        data = json.loads(dialog)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("turns"), list):
            return data["turns"]
    except Exception:
        pass
    return [{"role": "user", "content": dialog}]


def _err(code: str, msg: str) -> dict:
    return {"ok": False, "error_code": code, "error": msg}


# ---------------- 工具实现（可单测） ----------------

def record_impl(ctx: dict, dialog: str, source: str = "") -> dict:
    try:
        turns = _parse_dialog(dialog)
        facts = ctx["pipeline"].record(turns, source=source)
        return {"ok": True, "written": [f.to_dict() for f in facts], "count": len(facts)}
    except Exception as e:  # 主机不可达 / LLM 失败
        return _err("RECORD_FAILED", str(e))


def recall_impl(ctx: dict, query: str, top_k: int | None = None, stage: int = 3) -> dict:
    try:
        k = top_k or ctx.get("top_k", 5)
        results = ctx["fuse"].recall(query, top_k=k, stage=stage)
        return {"ok": True, "results": [r.to_dict() for r in results], "count": len(results)}
    except Exception as e:
        return _err("RECALL_FAILED", str(e))


def expand_impl_tool(ctx: dict, fact_id: str, hops: int = 1, max_results: int = 10) -> dict:
    try:
        facts = expand_impl(ctx["graph"], fact_id, hops=hops, max_results=max_results)
        return {"ok": True, "facts": [f.to_dict() for f in facts], "count": len(facts)}
    except Exception as e:
        return _err("EXPAND_FAILED", str(e))


def brief_impl(ctx: dict, max_tokens: int = 500) -> dict:
    try:
        ctx["views"].rebuild()
        brief_path = Path(ctx["root"]) / "vault_views" / "brief.md"
        text = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
        if max_tokens and len(text) > max_tokens * 2:   # 粗估 1 token ~ 2 字符
            text = text[: max_tokens * 2]
        return {"ok": True, "brief": text, "chars": len(text)}
    except Exception as e:
        return _err("BRIEF_FAILED", str(e))


def rebuild_impl(ctx: dict, verify: bool = False) -> dict:
    try:
        from liansu.rebuild import rebuild as rebuild_fn
        report = rebuild_fn(ctx["journal"], ctx["indexes"], ctx["views"], verify=verify)
        return {"ok": True, "report": report}
    except Exception as e:
        return _err("REBUILD_FAILED", str(e))


# ---------------- MCP 工具注册 ----------------

@mcp.tool
def record(dialog: str, source: str = "") -> dict:
    """写入记忆：当用户透露个人信息、偏好、决策、计划，或会话结束时调用。
    dialog 传对话 JSON（[{"role","content"},...]）或纯文本；自动 gate→抽取→去重→落盘。"""
    return record_impl(get_context(), dialog, source)


@mcp.tool
def recall(query: str, top_k: int = 5, stage: int = 3) -> dict:
    """召回记忆：涉及用户画像、历史决策、偏好、过往项目时先调用它。
    BM25+向量混合 RRF 融合，stage=3 默认含时效过滤（过期事实不返回）。"""
    return recall_impl(get_context(), query, top_k, stage)


@mcp.tool
def expand(fact_id: str, hops: int = 1, max_results: int = 10) -> dict:
    """关联扩散：需要顺着一条事实做多跳推理（同实体/同主题关联）时调用，hops 上限 3。"""
    return expand_impl_tool(get_context(), fact_id, hops, max_results)


@mcp.tool
def brief(max_tokens: int = 500) -> dict:
    """全局画像摘要：仅长任务开局或用户显式要求整体画像时调用，平时别滥用（会浪费 token）。"""
    return brief_impl(get_context(), max_tokens)


@mcp.tool
def rebuild(verify: bool = False) -> dict:
    """全量重建：索引或视图损坏、首次搭建、或改了 embedding 模型后调用，从 journal 真源重建一切。"""
    return rebuild_impl(get_context(), verify)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
