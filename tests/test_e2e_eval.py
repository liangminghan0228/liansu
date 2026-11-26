"""T15 端到端评测：语义匹配层 / 写路径 + 读路径联合判分 / 用例集校验。

重点不是「stub 能跑出满分」，而是**证明指标能抓到真实的坏情况**：
漏抽、多抽、谓词漂移导致 supersede 失效、gate 误放行、stage 过滤缺位。
"""
import json
from datetime import date
from pathlib import Path

import pytest

from eval.e2e_runner import (
    evaluate_case,
    load_cases,
    make_stub_provider,
    run_e2e,
    summarize,
    to_markdown_table,
)
from eval.fixture_provider import FixtureProvider
from eval.match import (
    fact_key,
    match_any,
    match_banned,
    match_spec,
    normalize,
    score_extraction,
)
from liansu.schema import Fact

CASES = Path(__file__).resolve().parents[1] / "eval" / "datasets" / "own" / "e2e.jsonl"


def _case(case_id: str) -> dict:
    return next(c for c in load_cases(CASES) if c["case_id"] == case_id)


def _fact(subject: str, predicate: str, object_: str) -> Fact:
    return Fact(
        id=f"F-20260101-{abs(hash((subject, predicate, object_))) % 10000:04d}",
        subject=subject, predicate=predicate, object=object_,
        statement=f"{subject} {predicate} {object_}",
        valid_from=date(2026, 1, 1), confidence=0.9,
    )


# ---------------------------------------------------------------- 归一化与匹配


def test_normalize_strips_punct_case_and_units():
    assert normalize("  82 公斤！") == "82kg"
    assert normalize("RAG / 多-Agent") == "rag多agent"
    assert normalize("80KG") == "80kg"


def test_normalize_keeps_decimal_point():
    # 不能把 82.5 归一成 825，否则和 825 撞车
    assert normalize("82.5") == "82.5"
    assert normalize("82kg") != normalize("82.5kg")


def test_match_strict_loose_none():
    spec = {"subject": "梁明晗", "predicate": "体重", "object": "82kg"}
    assert match_spec(_fact("梁明晗", "体重", "82 公斤"), spec) == "strict"
    # 谓词漂移：主客体对上 → loose（E2E_REPORT 里真实出现过的失败模式）
    assert match_spec(_fact("梁明晗", "重量", "82kg"), spec) == "loose"
    assert match_spec(_fact("梁明晗", "体重", "80kg"), spec) is None


def test_match_any_prefers_strict_over_loose():
    specs = [
        {"subject": "梁明晗", "predicate": "重量", "object": "82kg"},
        {"subject": "梁明晗", "predicate": "体重", "object": "82kg"},
    ]
    idx, mode = match_any(_fact("梁明晗", "体重", "82kg"), specs)
    assert (idx, mode) == (1, "strict")


def test_match_banned_finds_leaked_old_fact():
    banned = [{"subject": "梁明晗", "predicate": "体重", "object": "80kg"}]
    facts = [_fact("梁明晗", "体重", "80kg"), _fact("梁明晗", "体重", "82kg")]
    hits = match_banned(facts, banned)
    assert len(hits) == 1 and hits[0].object == "80kg"


def test_fact_key_handles_dict_and_object():
    spec = {"subject": "梁明晗", "predicate": "体重", "object": "82kg"}
    assert fact_key(spec) == fact_key(_fact("梁明晗", "体重", "82kg"))


# ---------------------------------------------------------------- 抽取打分


def test_score_extraction_perfect_and_drift():
    gold = [{"subject": "梁明晗", "predicate": "体重", "object": "82kg"}]
    exact = score_extraction([_fact("梁明晗", "体重", "82kg")], gold)
    assert (exact["precision"], exact["recall"], exact["predicate_match_rate"]) == (1.0, 1.0, 1.0)

    drift = score_extraction([_fact("梁明晗", "重量", "82kg")], gold)
    # 语义抽对了：P/R 仍 1.0，但谓词规范率 0 —— 写侧合并会失败
    assert (drift["precision"], drift["recall"], drift["predicate_match_rate"]) == (1.0, 1.0, 0.0)


def test_score_extraction_penalises_miss_and_noise():
    gold = [{"subject": "A", "predicate": "p", "object": "1"},
            {"subject": "A", "predicate": "q", "object": "2"}]
    partial = score_extraction([_fact("A", "p", "1"), _fact("A", "z", "9")], gold)
    assert partial["precision"] == pytest.approx(0.5)
    assert partial["recall"] == pytest.approx(0.5)
    assert len(partial["missed"]) == 1 and len(partial["extra"]) == 1


def test_score_extraction_empty_conventions():
    both_empty = score_extraction([], [])
    assert (both_empty["precision"], both_empty["recall"]) == (1.0, 1.0)
    assert score_extraction([], [{"subject": "A", "predicate": "p", "object": "1"}])["recall"] == 0.0
    assert score_extraction([_fact("A", "p", "1")], [])["precision"] == 0.0


# ---------------------------------------------------------------- 用例集校验


def test_cases_file_has_five_cases_with_unique_ids():
    cases = load_cases(CASES)
    assert len(cases) == 5
    ids = [c["case_id"] for c in cases]
    assert len(set(ids)) == 5
    for c in cases:
        assert c["dialog"], c["case_id"]
        for spec in c["gold_facts"] + c.get("must_not_include", []):
            assert {"subject", "predicate", "object"} <= set(spec)


# ---------------------------------------------------------------- 端到端链路


def test_stub_run_is_green_end_to_end(tmp_path):
    cases = load_cases(CASES)
    result = run_e2e(cases, make_stub_provider(cases), workdir=tmp_path / "run")
    assert result["summary"]["n_errors"] == 0

    by_id = {r["case_id"]: r for r in result["cases"]}
    for cid in ("e2e-01-name", "e2e-02-tech", "e2e-03-weight", "e2e-04-city"):
        assert by_id[cid]["gate_ok"] == 1.0
        assert by_id[cid]["hit"] == 1.0
    # 负样本：不该记的对话
    assert by_id["e2e-05-noise"]["gate_ok"] == 1.0
    assert by_id["e2e-05-noise"]["written"] == 0
    # 时效用例必须真的产生 supersede 且不残留旧值
    for cid in ("e2e-03-weight", "e2e-04-city"):
        assert by_id[cid]["supersede_ok"] == 1.0
        assert by_id[cid]["stale_conflict"] == 0.0
        assert by_id[cid]["temporal_ok"] == 1.0


def test_missing_fact_is_caught_by_recall(tmp_path):
    """用例 02 有三条 gold，抽漏一条 → 抽取 R 与 Recall@K 同时掉。"""
    case = _case("e2e-02-tech")
    provider = make_stub_provider([case])
    from eval.fixture_provider import dialog_key

    provider.table[dialog_key(case["dialog"])] = provider.table[dialog_key(case["dialog"])][:2]
    row = evaluate_case(case, provider, tmp_path / "c")
    assert row["extract"]["precision"] == 1.0
    assert row["extract"]["recall"] == pytest.approx(2 / 3)
    assert row["recall_at_k"] == pytest.approx(2 / 3)
    assert row["mrr"] == 1.0            # 抽到的仍在第一位，MRR 反映「排序」而非「完整」


def test_extra_fact_is_caught_by_precision(tmp_path):
    from eval.fixture_provider import dialog_key

    case = _case("e2e-01-name")
    provider = make_stub_provider([case])
    provider.table[dialog_key(case["dialog"])].append(
        {"subject": "用户", "predicate": "血型", "object": "O", "statement": "编造的血型"}
    )
    row = evaluate_case(case, provider, tmp_path / "c")
    assert row["extract"]["precision"] == pytest.approx(0.5)
    assert row["extract"]["recall"] == 1.0


def test_predicate_drift_is_visible_end_to_end(tmp_path):
    """谓词漂移：抽取语义对（loose 命中），但 find_by_key 不触发 supersede，
    旧值留在库里 → journal 残留 + 检索泄漏旧值。这正是纯检索评测看不到的失败。"""
    from eval.fixture_provider import dialog_key

    case = _case("e2e-03-weight")
    provider = make_stub_provider([case])
    main_key = dialog_key(case["dialog"])
    provider.table[main_key] = [
        {"subject": "用户", "predicate": "重量", "object": "82kg",
         "statement": "最近体测发现胖了，现在 82 公斤"}
    ]

    row = evaluate_case(case, provider, tmp_path / "c")
    assert row["extract"]["precision"] == 1.0          # 语义上抽对了
    assert row["extract"]["predicate_match_rate"] == 0.0
    assert row["supersede_ok"] == 0.0                  # 但写侧没合并
    assert row["stale_conflict"] == 1.0                # 两版共存
    assert row["temporal_ok"] == 0.0                   # 读侧把旧值也召回
    assert row["leaked_statements"]


def test_gate_false_positive_is_caught(tmp_path):
    """负样本被放行并落库 → gate 判 0。"""
    from eval.fixture_provider import dialog_key

    case = _case("e2e-05-noise")
    provider = make_stub_provider([case])
    provider.gates[dialog_key(case["dialog"])] = True
    provider.table[dialog_key(case["dialog"])] = [
        {"subject": "代码片段", "predicate": "内容", "object": "def f(x)", "statement": "一段函数"}
    ]
    row = evaluate_case(case, provider, tmp_path / "c")
    assert row["gate_ok"] == 0.0
    assert row["written"] == 1


def test_stage3_filters_superseded_fact_that_stage0_returns(tmp_path):
    """同一份数据：stage0 无时效过滤会召回旧事实，stage3 不会 —— 证明消融开关端到端有效。"""
    case = _case("e2e-03-weight")
    provider = make_stub_provider([case])

    row0 = evaluate_case(case, provider, tmp_path / "s0", stage=0)
    row3 = evaluate_case(case, provider, tmp_path / "s3", stage=3)
    assert row0["temporal_ok"] == 0.0      # 旧事实被召回
    assert row3["temporal_ok"] == 1.0      # 被时效过滤挡掉
    assert row3["hit"] == 1.0


def test_summary_skips_undefined_metrics(tmp_path):
    cases = load_cases(CASES)
    result = run_e2e(cases, make_stub_provider(cases), workdir=tmp_path / "run")
    summary = result["summary"]
    # 负样本没有检索/时效，样本数应小于用例总数
    assert summary["n_cases"] == 5
    assert summary["n_retrieval"] == 4
    assert summary["n_temporal"] == 2
    assert "| 用例 |" in to_markdown_table(result)
