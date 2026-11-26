"""T15 · 端到端评测：对话 → 抽取 → 落盘 / supersede → 建索引 → 召回 → 指标。

与 ``eval/runner.py``（纯检索消融）的分工：
- ``runner.py`` 假设 gold fact 已经在库里，只测「读路径」，抽取质量完全看不见；
- 本模块从**原始对话**出发，跑完整写路径再立刻测读路径，任何一个环节坏了
  都会在指标上掉下来：

      dialog ──▶ gate ──▶ extract ──▶ confidence ──▶ supersede ──▶ journal
                                                                    │
      question ──▶ recall(BM25+图+时效) ──▶ fact ──▶ 语义匹配 gold ◀┘

匹配规则见 ``eval/match.py``（strict = 三元组全等，loose = 主客体一致、容忍谓词漂移）。

用法（项目根执行）::

    # 冒烟：不联网，验证评测链路本身（抽取指标必然接近满分，不作能力结论）
    .venv/bin/python -m eval.e2e_runner --provider stub

    # 真实评测：走 config.yaml 里的 LLM
    .venv/bin/python -m eval.e2e_runner --provider real --json /tmp/e2e.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import traceback
from datetime import date
from pathlib import Path
from typing import Any, Sequence

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:  # 允许 `python -m eval.e2e_runner` 直接跑
    sys.path.insert(0, str(_SRC))

from eval.fixture_provider import FixtureProvider, dialog_key  # noqa: E402
from eval.match import match_any, match_banned, score_extraction  # noqa: E402

DEFAULT_CASES = Path("eval/datasets/own/e2e.jsonl")
DEFAULT_TOP_K = 5
DEFAULT_STAGE = 3

COLUMNS = ["用例", "gate", "抽取P", "抽取R", "谓词规范", "supersede", "旧值残留",
           "Hit@K", "Recall@K", "MRR", "时效", "抽取ms", "召回ms"]


# ---------------------------------------------------------------- 用例加载


def load_cases(path: str | Path) -> list[dict]:
    items: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _dialog(case: dict, field: str) -> list[dict[str, str]]:
    return case.get(field) or []


# ---------------------------------------------------------------- 环境装配


def build_env(root: Path, provider: Any, confidence_threshold: float) -> dict:
    """每个用例一套独立 vault，避免用例之间互相污染。"""
    from liansu.extract.pipeline import Pipeline
    from liansu.index.bm25 import BM25Index
    from liansu.index.fuse import FuseIndex
    from liansu.index.graph import GraphIndex
    from liansu.vault.journal import Journal

    journal = Journal(root / "journal")
    bm25, graph = BM25Index(), GraphIndex()
    indexes = {"bm25": bm25, "graph": graph}
    pipeline = Pipeline(
        journal,
        provider,
        indexes=[bm25, graph],
        confidence_threshold=confidence_threshold,
        state_path=root / ".liansu" / "processed.json",
    )
    fuse = FuseIndex(journal, indexes)
    return {"journal": journal, "pipeline": pipeline, "indexes": indexes, "fuse": fuse}


def make_stub_provider(cases: Sequence[dict]) -> FixtureProvider:
    """用标注拼 stub 表：pre_dialog 的理想抽取 = must_not_include（就是被覆盖的旧事实）。"""
    table: dict[str, list[dict[str, Any]]] = {}
    gates: dict[str, bool] = {}
    for case in cases:
        pre, dialog = _dialog(case, "pre_dialog"), _dialog(case, "dialog")
        pre_date = case.get("pre_date") or date.today().isoformat()
        if pre:
            table[dialog_key(pre)] = [
                {**spec, "valid_from": pre_date} for spec in case.get("must_not_include", [])
            ]
        main_date = case.get("date") or date.today().isoformat()
        table[dialog_key(dialog)] = [{**spec, "valid_from": main_date} for spec in case.get("gold_facts", [])]
        if case.get("expect") == "skip":
            gates[dialog_key(dialog)] = False
    return FixtureProvider(table, gates=gates)


# ---------------------------------------------------------------- 单例评测


def _recall_metrics(
    fuse: Any, journal: Any, gold_specs: Sequence[dict], banned_specs: Sequence[dict],
    question: str, top_k: int, stage: int, when: date,
) -> dict:
    """召回结果映射回 gold 规格空间算指标（抽取出的 id 是随机生成的，不能按 id 比）。"""
    results = fuse.recall(question, top_k=top_k, stage=stage, when=when)
    ids = [r.fact_id for r in results]
    facts_by_id = {f.id: f for f in journal.replay()}

    spec_rank: dict[int, int] = {}
    for rank, fid in enumerate(ids, start=1):
        fact = facts_by_id.get(fid)
        if fact is None:
            continue
        idx, _mode = match_any(fact, gold_specs)
        if idx is not None and idx not in spec_rank:
            spec_rank[idx] = rank

    recalled_facts = [facts_by_id[i] for i in ids if i in facts_by_id]
    leaked = match_banned(recalled_facts, banned_specs) if banned_specs else []

    return {
        "top_ids": ids,
        "hit": 1.0 if spec_rank else 0.0,
        "recall_at_k": (len(spec_rank) / len(gold_specs)) if gold_specs else None,
        "mrr": (1.0 / min(spec_rank.values())) if spec_rank else 0.0,
        "temporal_ok": (0.0 if leaked else 1.0) if banned_specs else None,
        "leaked_statements": [f.statement for f in leaked],
        "recalled": recalled_facts,
    }


def evaluate_case(
    case: dict,
    provider: Any,
    root: Path,
    confidence_threshold: float = 0.7,
    top_k: int = DEFAULT_TOP_K,
    stage: int = DEFAULT_STAGE,
    when: date | None = None,
    judge: bool = False,
) -> dict:
    """跑一个用例，返回该用例的全部指标（失败时 error 非空，其余为 None）。"""
    when = when or date.today()
    cid = case.get("case_id", "?")
    out: dict[str, Any] = {
        "case_id": cid, "kind": case.get("kind", "fact"), "expect": case.get("expect", "record"),
        "error": None, "gate_ok": None, "written": 0, "extract": None,
        "old_facts_found": 0, "old_fact_missing": False, "supersede_ok": None,
        "link_ok": None, "stale_conflict": None,
        "hit": None, "recall_at_k": None, "mrr": None, "temporal_ok": None,
        "leaked_statements": [], "extract_ms": None, "recall_ms": None, "judge_score": None,
    }

    env = build_env(root, provider, confidence_threshold)
    journal, pipeline = env["journal"], env["pipeline"]
    gold_specs = case.get("gold_facts", [])
    banned_specs = case.get("must_not_include", [])

    try:
        started = time.perf_counter()
        if _dialog(case, "pre_dialog"):
            pipeline.record(_dialog(case, "pre_dialog"), source=f"{cid}:pre")
            pre_elapsed = (time.perf_counter() - started) * 1000.0
        else:
            pre_elapsed = 0.0
        started = time.perf_counter()
        written = pipeline.record(_dialog(case, "dialog"), source=f"{cid}:main")
        extract_ms = pre_elapsed + (time.perf_counter() - started) * 1000.0
    except Exception as exc:  # LLM 不可达 / 抽取解析失败：标错但不拖垮整轮
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    facts = journal.replay()
    out["written"] = len(written)
    out["extract_ms"] = extract_ms

    # ---- 写路径：gate / 抽取质量
    if out["expect"] == "skip":
        out["gate_ok"] = 0.0 if written else 1.0
    else:
        out["gate_ok"] = 1.0 if written else 0.0
        out["extract"] = score_extraction(written, gold_specs)

    # ---- 写路径：supersede 是否真的发生（只看 journal 侧，不看检索）
    old_facts = match_banned(facts, banned_specs) if banned_specs else []
    out["old_facts_found"] = len(old_facts)
    if banned_specs and old_facts:
        invalidated = [f for f in old_facts if f.valid_to is not None]
        out["supersede_ok"] = 1.0 if len(invalidated) == len(old_facts) else 0.0
        out["stale_conflict"] = 0.0 if len(invalidated) == len(old_facts) else 1.0
        out["link_ok"] = 1.0 if any(f.supersedes for f in written) else 0.0
    elif banned_specs:
        # 旧事实压根没入库（例如 confidence 被 0.7 阈值挡掉）：没有可覆盖的东西，
        # supersede 记为 n/a 而不是 0 分，否则会把「该记的新事实写对了」也算成故障。
        out["supersede_ok"] = None
        out["old_fact_missing"] = True
        out["stale_conflict"] = None

    # ---- 读路径：重建索引后按问题召回
    question = case.get("question") or ""
    if question.strip():
        for index in env["indexes"].values():
            index.rebuild(facts)
        started = time.perf_counter()
        rm = _recall_metrics(env["fuse"], journal, gold_specs, banned_specs, question, top_k, stage, when)
        out["recall_ms"] = (time.perf_counter() - started) * 1000.0
        for key in ("hit", "recall_at_k", "mrr", "temporal_ok", "leaked_statements"):
            out[key] = rm[key]
        if judge:
            try:
                verdict = provider.judge(question, rm["recalled"], case.get("answer", ""))
                score = next((verdict[k] for k in ("score", "overall", "faithfulness")
                              if isinstance(verdict.get(k), (int, float))), None)
                out["judge_score"] = float(score) if score is not None else None
            except Exception:
                out["judge_score"] = None
    return out


def run_e2e(
    cases: Sequence[dict],
    provider: Any,
    confidence_threshold: float = 0.7,
    top_k: int = DEFAULT_TOP_K,
    stage: int = DEFAULT_STAGE,
    when: date | None = None,
    judge: bool = False,
    workdir: Path | None = None,
    keep: bool = False,
) -> dict:
    """逐用例跑端到端评测，返回 {"cases": [...], "summary": {...}}。"""
    base = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="liansu-e2e-"))
    base.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    try:
        for i, case in enumerate(cases):
            rows.append(
                evaluate_case(
                    case, provider, base / f"case-{i:02d}",
                    confidence_threshold=confidence_threshold, top_k=top_k,
                    stage=stage, when=when, judge=judge,
                )
            )
    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
    return {"cases": rows, "summary": summarize(rows, top_k=top_k)}

# ---------------------------------------------------------------- 聚合与展示


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(rows: Sequence[dict], top_k: int = DEFAULT_TOP_K) -> dict:
    """跨用例聚合：只对「有定义」的样本求均值，并记录各指标的样本数。"""
    done = [r for r in rows if not r.get("error")]

    def collect(fn) -> list[float]:
        return [v for r in done if (v := fn(r)) is not None]

    gate = collect(lambda r: r["gate_ok"])
    precision = collect(lambda r: r["extract"]["precision"] if r["extract"] else None)
    recall = collect(lambda r: r["extract"]["recall"] if r["extract"] else None)
    f1 = collect(lambda r: r["extract"]["f1"] if r["extract"] else None)
    pred_rate = collect(lambda r: r["extract"]["predicate_match_rate"] if r["extract"] else None)
    supersede = collect(lambda r: r["supersede_ok"])
    stale = collect(lambda r: r["stale_conflict"])
    hit = collect(lambda r: r["hit"])
    recall_k = collect(lambda r: r["recall_at_k"])
    rr = collect(lambda r: r["mrr"])
    temporal = collect(lambda r: r["temporal_ok"])
    judge = collect(lambda r: r["judge_score"])

    return {
        "n_cases": len(rows),
        "n_errors": len(rows) - len(done),
        "n_gate": len(gate),
        "n_extract": len(precision),
        "n_supersede": len(supersede),
        "n_retrieval": len(hit),
        "n_temporal": len(temporal),
        "top_k": top_k,
        "gate_accuracy": _mean(gate),
        "extract_precision": _mean(precision),
        "extract_recall": _mean(recall),
        "extract_f1": _mean(f1),
        "predicate_match_rate": _mean(pred_rate),
        "supersede_rate": _mean(supersede),
        "stale_conflict_rate": _mean(stale),
        "hit_rate": _mean(hit),
        "recall_at_k": _mean(recall_k),
        "mrr": _mean(rr),
        "temporal_accuracy": _mean(temporal),
        "avg_extract_ms": _mean(collect(lambda r: r["extract_ms"])),
        "avg_recall_ms": _mean(collect(lambda r: r["recall_ms"])),
        "avg_judge_score": _mean(judge),
    }


def _fmt(value: Any, digits: int) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.{digits}f}"
    return "—"


def to_markdown_table(result: dict) -> str:
    rows = result.get("cases", [])
    summary = result.get("summary", {})
    k = summary.get("top_k", DEFAULT_TOP_K)

    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['case_id']} | ⚠️ {r['error'][:40]} | | | | | | | | | | | |")
            continue
        ext = r.get("extract") or {}
        cells = [
            r["case_id"],
            _fmt(r.get("gate_ok"), 0),
            _fmt(ext.get("precision"), 2),
            _fmt(ext.get("recall"), 2),
            _fmt(ext.get("predicate_match_rate"), 2),
            _fmt(r.get("supersede_ok"), 0),
            _fmt(r.get("stale_conflict"), 0),
            _fmt(r.get("hit"), 0),
            _fmt(r.get("recall_at_k"), 2),
            _fmt(r.get("mrr"), 2),
            _fmt(r.get("temporal_ok"), 0),
            _fmt(r.get("extract_ms"), 0),
            _fmt(r.get("recall_ms"), 0),
        ]
        lines.append("| " + " | ".join(cells) + " |")

    means = [
        "**均值**",
        _fmt(summary.get("gate_accuracy"), 2),
        _fmt(summary.get("extract_precision"), 2),
        _fmt(summary.get("extract_recall"), 2),
        _fmt(summary.get("predicate_match_rate"), 2),
        _fmt(summary.get("supersede_rate"), 2),
        _fmt(summary.get("stale_conflict_rate"), 2),
        _fmt(summary.get("hit_rate"), 2),
        _fmt(summary.get("recall_at_k"), 2),
        _fmt(summary.get("mrr"), 2),
        _fmt(summary.get("temporal_accuracy"), 2),
        _fmt(summary.get("avg_extract_ms"), 0),
        _fmt(summary.get("avg_recall_ms"), 0),
    ]
    lines.append("| " + " | ".join(means) + " |")
    lines.append("")
    lines.append(
        f"样本数：gate {summary.get('n_gate')}｜抽取 {summary.get('n_extract')}｜"
        f"supersede {summary.get('n_supersede')}｜检索 {summary.get('n_retrieval')}（top_k={k}）｜"
        f"时效 {summary.get('n_temporal')}｜失败 {summary.get('n_errors')}"
    )
    return "\n".join(lines)

# ---------------------------------------------------------------- CLI


def _build_provider(kind: str, cases: Sequence[dict]):
    if kind == "stub":
        return make_stub_provider(cases)
    from liansu.config import load_config
    from liansu.providers.api import ApiProvider

    return ApiProvider(config=load_config())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval.e2e_runner", description="连溯端到端评测（抽取→检索）")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="端到端用例 jsonl 路径")
    parser.add_argument("--provider", choices=("stub", "real"), default="stub",
                        help="stub=离线冒烟（不测模型能力），real=走 config.yaml 的真实 LLM")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--stage", type=int, default=DEFAULT_STAGE, help="召回 stage：0 BM25 / 3 加时效")
    parser.add_argument("--threshold", type=float, default=0.7, help="抽取 confidence 阈值")
    parser.add_argument("--judge", action="store_true", help="额外调 LLM judge 打分（仅 real）")
    parser.add_argument("--json", default=None, help="把逐例结果与汇总另存为 JSON")
    parser.add_argument("--keep", action="store_true", help="保留临时 vault，便于排查")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if not cases:
        print(f"[error] 用例集为空：{args.cases}", file=sys.stderr)
        return 1

    provider = _build_provider(args.provider, cases)
    try:
        result = run_e2e(
            cases, provider, confidence_threshold=args.threshold, top_k=args.top_k,
            stage=args.stage, judge=args.judge, keep=args.keep,
        )
    except Exception:
        traceback.print_exc()
        return 2

    print(f"用例集：{args.cases}（{len(cases)} 条）  provider={args.provider}  "
          f"top_k={args.top_k}  stage={args.stage}  confidence={args.threshold}")
    if args.provider == "stub":
        print("⚠️ stub 通道用标注喂抽取，只验证评测链路，抽取指标不作模型能力结论")
    print()
    print(to_markdown_table(result))
    if result["summary"].get("avg_judge_score") is not None:
        print(f"\nJudge 均值：{result['summary']['avg_judge_score']:.3f}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"\n结果已写入 {args.json}")

    return 1 if result["summary"]["n_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
