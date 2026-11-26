"""T15 · 评测执行器 + 分阶段消融。

对自建 gold 集逐条跑 ``FuseIndex.recall``，按 Stage 聚合指标并输出对比表：

  Stage0 仅 BM25 → Stage1 +向量 → Stage2 +图 → Stage3 +时效过滤（当前默认）

用法（在项目根执行）：

    .venv/bin/python -m eval.runner --gold eval/datasets/own/gold.jsonl --stages 0,1,2,3 --top-k 5

评测不强依赖 LLM：不传 provider 时跳过 judge，只算检索/时效指标。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:  # 允许 `python -m eval.runner` 直接跑，无需先 pip install
    sys.path.insert(0, str(_SRC))

from eval.metrics import hit_rate, mrr, recall_at_k, temporal_accuracy  # noqa: E402

DEFAULT_GOLD = Path("eval/datasets/own/gold.jsonl")
DEFAULT_STAGES: tuple[int, ...] = (0, 1, 2, 3)
DEFAULT_TOP_K = 5

STAGE_LABELS = {
    0: "0 · BM25",
    1: "1 · +向量",
    2: "2 · +图",
    3: "3 · +时效",
}


# ---------------------------------------------------------------- gold 集


def load_gold(path: str | Path) -> list[dict]:
    """读取 jsonl gold 集，跳过空行。

    每行形如 ``{question, gold_facts[], answer, source, must_not_include?}``。
    """
    items: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def _gold_ids(item: dict) -> set[str]:
    """gold_facts 是完整 fact dict，这里只取 id。"""
    return {
        f["id"]
        for f in item.get("gold_facts", [])
        if isinstance(f, dict) and f.get("id")
    }


def _banned_ids(item: dict) -> set[str]:
    """must_not_include：已被 supersede 的旧 fact id。"""
    return {str(x) for x in item.get("must_not_include", []) or []}


# ---------------------------------------------------------------- 聚合


def _mean(values: Iterable[float]) -> float:
    xs = [float(v) for v in values]
    return sum(xs) / len(xs) if xs else 0.0


def _fact_lookup(fuse: Any) -> dict[str, Any]:
    """从 fuse.journal 取 id -> Fact 映射，供 judge 使用；取不到则返回空 dict。"""
    journal = getattr(fuse, "journal", None)
    if journal is None:
        return {}
    try:
        return {f.id: f for f in journal.replay()}
    except Exception:  # journal 不可用不应阻断检索评测
        return {}


def _judge_score(provider: Any, question: str, facts: list, answer: str) -> float | None:
    """调 provider.judge 取分数；judge 失败返回 None（不影响检索指标）。"""
    if provider is None:
        return None
    try:
        result = provider.judge(question, facts, answer)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    for key in ("score", "overall", "faithfulness"):
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _eval_stage(
    fuse: Any,
    gold: Sequence[dict],
    stage: int,
    top_k: int,
    facts_by_id: dict[str, Any] | None = None,
    provider: Any = None,
    when: date | None = None,
) -> dict:
    """跑一个 Stage 的全部 question，返回该 Stage 的指标汇总。"""
    hits: list[float] = []
    recalls: list[float] = []
    mrrs: list[float] = []
    temporals: list[float] = []
    latencies: list[float] = []
    judge_scores: list[float] = []

    for item in gold:
        question = item.get("question", "")
        gold_ids = _gold_ids(item)
        banned = _banned_ids(item)

        started = time.perf_counter()
        results = fuse.recall(question, top_k=top_k, stage=stage, when=when)
        latencies.append((time.perf_counter() - started) * 1000.0)

        ids = [r.fact_id for r in results]

        hits.append(hit_rate(ids, gold_ids))
        recalls.append(recall_at_k(ids, gold_ids, top_k))
        mrrs.append(mrr(ids, gold_ids))
        if banned:  # 只统计有「旧事实陷阱」的 query
            temporals.append(temporal_accuracy(ids, banned))

        score = _judge_score(
            provider,
            question,
            [facts_by_id[i] for i in ids if facts_by_id and i in facts_by_id],
            item.get("answer", ""),
        )
        if score is not None:
            judge_scores.append(score)

    return {
        "stage": stage,
        "top_k": top_k,
        "n": len(gold),
        "n_temporal": len(temporals),
        "n_judged": len(judge_scores),
        "hit_rate": _mean(hits),
        "recall": _mean(recalls),
        "recall_at": top_k,
        "mrr": _mean(mrrs),
        "temporal_accuracy": _mean(temporals),
        "avg_latency_ms": _mean(latencies),
        "avg_tokens": None,  # 占位：recall 路径无 LLM 调用，token 成本待接入生成/重排后统计
        "avg_judge_score": _mean(judge_scores) if judge_scores else None,
    }


def run_ablation(
    fuse: Any,
    gold: Sequence[dict],
    stages: Sequence[int] = DEFAULT_STAGES,
    top_k: int = DEFAULT_TOP_K,
    provider: Any = None,
    when: date | None = None,
) -> dict[int, dict]:
    """逐 Stage 跑全量 gold，返回 {stage: 指标汇总}。"""
    facts_by_id = _fact_lookup(fuse) if provider is not None else {}
    return {
        int(stage): _eval_stage(
            fuse, gold, int(stage), top_k, facts_by_id=facts_by_id, provider=provider, when=when
        )
        for stage in stages
    }


# ---------------------------------------------------------------- 输出


def _fmt(value: Any, digits: int) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.{digits}f}"
    return "—"


def to_markdown_table(results: dict) -> str:
    """把 run_ablation 的结果渲染成 Markdown 对比表。"""
    if not results:
        return "（无结果）"

    rows = [results[k] for k in sorted(results, key=lambda s: int(s))]
    recall_at = rows[0].get("recall_at", "K")
    has_judge = any(r.get("avg_judge_score") is not None for r in rows)

    headers = ["Stage", "HitRate", f"Recall@{recall_at}", "MRR", "时效正确率", "平均延迟(ms)"]
    if has_judge:
        headers.append("Judge")

    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
    ]
    for r in rows:
        stage = r.get("stage")
        cells = [
            STAGE_LABELS.get(stage, str(stage)),
            _fmt(r.get("hit_rate"), 3),
            _fmt(r.get("recall"), 3),
            _fmt(r.get("mrr"), 3),
            _fmt(r.get("temporal_accuracy"), 3),
            _fmt(r.get("avg_latency_ms"), 1),
        ]
        if has_judge:
            cells.append(_fmt(r.get("avg_judge_score"), 3))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI


def build_fuse(vault: str | Path | None = None):
    """用 journal 真源重建 BM25 + 图索引，组装 FuseIndex。

    向量路需要 Embedder 实现（bge-small-zh），当前仓库尚未提供，故 Stage1 与
    Stage0 结果相同；接入 embedder 后在此处补 VectorIndex 即可，评测代码无需改动。
    """
    from liansu.index.bm25 import BM25Index
    from liansu.index.fuse import FuseIndex
    from liansu.vault.journal import Journal

    journal = Journal(vault) if vault else Journal()
    facts = journal.replay()  # 含被 supersede 的旧事实，时效指标才有区分度

    indexes: dict = {}
    bm25 = BM25Index()
    bm25.rebuild(facts)
    indexes["bm25"] = bm25

    try:
        from liansu.index.graph import GraphIndex

        graph = GraphIndex()
        graph.rebuild(facts)
        indexes["graph"] = graph
    except Exception as exc:  # networkx 缺失时退化为无图消融
        print(f"[warn] 图索引不可用，Stage2 将等价于 Stage1：{exc}", file=sys.stderr)

    return FuseIndex(journal, indexes)


def _parse_stages(raw: str) -> list[int]:
    stages = [int(x) for x in raw.replace(" ", "").split(",") if x]
    if not stages:
        raise argparse.ArgumentTypeError("stages 不能为空")
    return stages


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval.runner", description="连溯检索消融评测")
    parser.add_argument("--gold", default=str(DEFAULT_GOLD), help="gold jsonl 路径")
    parser.add_argument("--stages", default="0,1,2,3", help="逗号分隔的 Stage 列表")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="每 query 取前 k 条")
    parser.add_argument("--vault", default=None, help="journal 目录，默认项目根 journal/")
    parser.add_argument("--json", default=None, help="把各 Stage 指标另存为 JSON 文件")
    args = parser.parse_args(argv)

    gold = load_gold(args.gold)
    if not gold:
        print(f"[error] gold 集为空：{args.gold}", file=sys.stderr)
        return 1

    fuse = build_fuse(args.vault)
    results = run_ablation(fuse, gold, stages=_parse_stages(args.stages), top_k=args.top_k)

    print(f"gold 集：{args.gold}（{len(gold)} 条）  top_k={args.top_k}")
    print()
    print(to_markdown_table(results))

    if args.json:
        Path(args.json).write_text(
            json.dumps({str(k): v for k, v in results.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n指标已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
