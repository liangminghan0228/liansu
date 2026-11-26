"""公开数据集评测：把 gold 事实灌进隔离 journal，再跑与自建集同一套 Stage 消融。

为什么需要这一层（与 ``eval.runner`` 的分工）：

- ``eval.runner`` 假定 fact **已在 vault 里**，只跑 ``build_fuse(vault)`` + 检索；
- 公开集（LongMemEval / LoCoMo / HotpotQA / FreshQA）的事实只存在于数据集本身，
  必须先按 ``valid_from`` 写进一个临时 journal，索引才有东西可召回；
- 灌库用临时目录、跑完即删，绝不碰用户真实 vault。

隔离粒度 ``--isolate``：

- ``case``（默认）：每题一个独立 journal。公开集的题彼此独立，共用一个索引会让
  「命中别题的事实」既可能抬高也可能压低分数，逐题隔离后指标才有可比性；
- ``file``：整个数据集一个 journal，接近真实「长期积累」的检索条件，但存在跨题污染，
  仅用于对照。

判分口径（与 ``docs/evalset.md`` 一致）：

- ``expect=abstain`` 的对抗样本不参与检索指标，只计数；
- ``scope=temporal`` 的记录（FreshQA 无证据原文）不参与 HitRate/MRR 聚合；
- 时效正确率只统计带 ``must_not_include`` 的记录。

用法::

    .venv/bin/python -m eval.public_runner --public-dir eval/datasets/public
    .venv/bin/python -m eval.public_runner --datasets locomo --isolate file --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

# 复用 eval.runner 的组装/消融/渲染，避免第二套判分口径
from eval.datasets.public.convert import read_jsonl, validate_records
from eval.runner import build_fuse, run_ablation, to_markdown_table

DEFAULT_PUBLIC_DIR = Path("eval/datasets/public")
DEFAULT_STAGES: tuple[int, ...] = (0, 1, 2, 3)
DEFAULT_TOP_K = 5


# ---------------------------------------------------------------- 灌库


def _fact_rows(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """摊平 gold + stale fact（不改原记录）。"""
    return [
        f
        for rec in records
        for f in list(rec.get("gold_facts") or []) + list(rec.get("stale_facts") or [])
    ]


def _write_journal(facts: Iterable[dict[str, Any]], root: Path) -> int:
    """把 fact 写进 ``root``，按 id 去重（gold 与 haystack 指向同一轮时只写一次）。"""
    from liansu.schema import Fact
    from liansu.vault.journal import Journal

    journal = Journal(root)
    seen: set[str] = set()
    for fact in facts:
        if fact["id"] in seen:
            continue
        seen.add(fact["id"])
        journal.append(Fact.from_dict(fact))
    return len(seen)


def ingest(records: Iterable[dict[str, Any]], root: Path) -> int:
    """把 gold + stale fact 写进 ``root`` 下的临时 journal，返回写入条数。

    stale_facts 由转换器给出 ``valid_to``，直接以该状态落盘：重放时它天然过期，
    Stage3 的时效过滤会剔除它 —— 这正是「旧值不该被召回」的检验点。
    """
    return _write_journal(_fact_rows(records), root)


def load_haystack(path: Path) -> dict[str, list[dict[str, Any]]]:
    """读 ``<dataset>.haystack.jsonl``：每行 ``{sample_id, facts}``。

    有 haystack 的数据集（LoCoMo）每道题在整场会话里检索；gold 与 haystack
    是同一批 fact（id 相同），所以召回命中就是真的找到了那几轮。
    """
    if not path.exists():
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[str(row["sample_id"])] = list(row.get("facts") or [])
    return out


def resolve_isolation(isolate: str, haystack: dict[str, list[dict[str, Any]]]) -> str:
    """``auto`` 的语义：有 haystack 就按场次隔离，否则逐题隔离。"""
    if isolate != "auto":
        if isolate in ("sample", "file") and not haystack:
            # 没有 haystack 时 sample 无意义，退化为 file（整场就是全部题干）
            return "file"
        return isolate
    return "sample" if haystack else "case"


def _groups(records: Sequence[dict[str, Any]], mode: str) -> list[list[dict[str, Any]]]:
    """按隔离粒度分组：case=每题，sample=每场会话，file=全数据集。"""
    if mode == "case":
        return [[r] for r in records]
    if mode == "file":
        return [list(records)]
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        buckets[str((rec.get("meta") or {}).get("sample_id") or "_all")].append(rec)
    return list(buckets.values())


# ---------------------------------------------------------------- 聚合


def _new_acc() -> dict[str, list[float]]:
    return {
        "hit": [], "recall": [], "mrr": [], "temporal": [], "latency": [],
        "n_scored": 0, "abstain": 0,
    }


def _accumulate(
    acc: dict[int, dict[str, list[float]]],
    record: dict[str, Any],
    per_stage: dict[int, dict[str, Any]],
) -> None:
    """把一条记录的逐 Stage 指标并入累加器。"""
    expect = record.get("expect", "answer")
    scope = record.get("scope", "retrieval")
    for stage, summary in per_stage.items():
        bucket = acc[stage]
        bucket["latency"].append(summary["avg_latency_ms"])
        if summary["n_temporal"]:
            bucket["temporal"].append(summary["temporal_accuracy"])
        if expect == "abstain":
            bucket["abstain"] += 1
            continue
        if scope != "retrieval":
            continue  # temporal-only 数据集不进检索指标（也不计分母）
        bucket["n_scored"] += 1
        bucket["hit"].append(summary["hit_rate"])
        bucket["recall"].append(summary["recall"])
        bucket["mrr"].append(summary["mrr"])


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(acc: dict[int, dict[str, Any]], top_k: int) -> dict[int, dict[str, Any]]:
    """把累加器渲染成与 ``to_markdown_table`` 兼容的指标字典。

    没有检索口径样本时（如 FreshQA 只评时效）把比率置 ``None``，
    渲染成「—」而不是 0.0 —— 0.0 会被误读成「全错」。
    """
    out: dict[int, dict[str, Any]] = {}
    for stage, bucket in sorted(acc.items()):
        scored = bucket["hit"]
        out[stage] = {
            "stage": stage,
            "top_k": top_k,
            "n": bucket["n_scored"],
            "n_temporal": len(bucket["temporal"]),
            "n_abstain": bucket["abstain"],
            "hit_rate": _mean(scored) if scored else None,
            "recall": _mean(bucket["recall"]) if scored else None,
            "recall_at": top_k,
            "mrr": _mean(bucket["mrr"]) if scored else None,
            "temporal_accuracy": _mean(bucket["temporal"]) if bucket["temporal"] else None,
            "avg_latency_ms": _mean(bucket["latency"]),
            "avg_tokens": None,
            "avg_judge_score": None,
        }
    return out


def evaluate(
    records: Sequence[dict[str, Any]],
    stages: Sequence[int] = DEFAULT_STAGES,
    top_k: int = DEFAULT_TOP_K,
    isolate: str = "auto",
    haystack: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[int, dict[str, Any]]:
    """跑一个公开集，返回 ``{stage: 累加器}``。

    不直接返回渲染后的表，是为了让调用方能把多个数据集合并成「公开集汇总」。
    """
    haystack = haystack or {}
    mode = resolve_isolation(isolate, haystack)

    acc: dict[int, dict[str, Any]] = defaultdict(_new_acc)
    for group in _groups(records, mode):
        facts = _fact_rows(group)
        if mode == "sample":
            for rec in group:
                facts.extend(haystack.get(str((rec.get("meta") or {}).get("sample_id")), []))
        elif mode == "file":
            for sample_facts in haystack.values():
                facts.extend(sample_facts)
        with tempfile.TemporaryDirectory(prefix="liansu-public-eval-") as tmp:
            _write_journal(facts, Path(tmp))
            fuse = build_fuse(tmp)
            for record in group:
                per_stage = run_ablation(fuse, [record], stages=stages, top_k=top_k)
                _accumulate(acc, record, per_stage)
    return {int(k): dict(v) for k, v in acc.items()}


def merge(accs: Iterable[dict[int, dict[str, Any]]]) -> dict[int, dict[str, Any]]:
    """合并多个数据集的累加器（用于汇总表）。"""
    merged: dict[int, dict[str, Any]] = defaultdict(_new_acc)
    for acc in accs:
        for stage, bucket in acc.items():
            target = merged[stage]
            for key in ("hit", "recall", "mrr", "temporal", "latency"):
                target[key].extend(bucket[key])
            target["n_scored"] += bucket["n_scored"]
            target["abstain"] += bucket["abstain"]
    return {int(k): dict(v) for k, v in merged.items()}


# ---------------------------------------------------------------- IO / CLI


def discover(public_dir: Path) -> list[Path]:
    """按约定命名 ``<dataset>.gold.jsonl`` 发现已转换的公开集。"""
    return sorted(public_dir.glob("*.gold.jsonl"))


def _render(title: str, summary: dict[int, dict[str, Any]]) -> str:
    head = (
        f"### {title}\n\n"
        f"检索口径 {summary[min(summary)]['n']} 题｜时效样本 "
        f"{summary[min(summary)]['n_temporal']} 题｜对抗样本 "
        f"{summary[min(summary)]['n_abstain']} 题\n"
    )
    return head + "\n" + to_markdown_table(summary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval.public_runner", description="公开数据集检索消融评测（隔离 journal）"
    )
    parser.add_argument("--public-dir", default=str(DEFAULT_PUBLIC_DIR), help="转换后的公开集目录")
    parser.add_argument("--datasets", default=None, help="逗号分隔的数据集名，默认全部 *.gold.jsonl")
    parser.add_argument("--stages", default="0,1,2,3", help="逗号分隔的 Stage 列表")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="每 query 取前 k 条")
    parser.add_argument(
        "--isolate", default="auto", choices=("auto", "case", "sample", "file"),
        help="隔离粒度：auto=有 haystack 按场次，否则逐题；case=每题；sample=每场会话；file=整个数据集",
    )
    parser.add_argument("--json", default=None, help="把指标另存为 JSON 文件")
    args = parser.parse_args(argv)

    stages = [int(x) for x in args.stages.replace(" ", "").split(",") if x]
    public_dir = Path(args.public_dir)
    files = discover(public_dir)
    if args.datasets:
        wanted = {x.strip() for x in args.datasets.split(",") if x.strip()}
        files = [f for f in files if f.name.split(".")[0] in wanted]
    if not files:
        print(
            f"[error] {public_dir} 下没有 *.gold.jsonl；先跑 "
            f"python -m eval.datasets.public.convert --dataset all --input-dir raw/",
            file=sys.stderr,
        )
        return 1

    results: dict[str, dict[str, dict[str, Any]]] = {}
    accs: list[dict[int, dict[str, Any]]] = []
    for path in files:
        dataset = path.name.split(".")[0]
        records = read_jsonl(path)
        errors = validate_records(records)
        if errors:
            for err in errors[:10]:
                print(f"[error] {dataset}: {err}", file=sys.stderr)
            return 1

        haystack = load_haystack(path.with_name(path.name.replace(".gold.jsonl", ".haystack.jsonl")))
        mode = resolve_isolation(args.isolate, haystack)
        acc = evaluate(records, stages=stages, top_k=args.top_k, isolate=mode, haystack=haystack)
        accs.append(acc)
        summary = summarize(acc, args.top_k)
        results[dataset] = summary
        scope_note = ""
        if haystack and mode in ("sample", "file"):
            rows = sum(len(v) for v in haystack.values())
            scope_note = f"｜haystack {len(haystack)} 场 / {rows} 轮"
        print(_render(f"{dataset}（{path.name}｜isolate={mode}{scope_note}）", summary))
        print()

    if len(results) > 1:
        overall = summarize(merge(accs), args.top_k)
        results["_overall"] = overall
        print(_render("公开集汇总（跨数据集）", overall))
        print()

    print(
        "口径提醒：gold 事实来自数据集证据句，只评检索+时效（不评抽取）；"
        "无 embedder 时 Stage1 与 Stage0 等价，BM25 纯词面召回在英文公开集上会系统性偏低。"
    )

    if args.json:
        Path(args.json).write_text(
            json.dumps({k: {str(s): v for s, v in summ.items()} for k, summ in results.items()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n指标已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
