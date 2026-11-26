"""公开数据集的转换与评测（T14-T15 扩展）单测。

三块覆盖：
1. 转换器：原始格式 → 统一格式，证据映射、日期解析、旧值/对抗样本标注；
2. 校验器：坏数据必须报错（stale 缺 valid_to、旧 id 混进 gold）；
3. 评测器：隔离 journal 灌库 → Stage 消融，旧事实 Stage3 被过滤、逐题隔离生效。
"""
from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

import pytest

from eval.datasets.public.convert import (
    FRESHQA_CUTOFF,
    PLACEHOLDER_DATE,
    convert_freshqa,
    convert_hotpotqa,
    convert_locomo,
    convert_longmemeval,
    load_raw,
    make_fact,
    parse_date,
    read_jsonl,
    validate_record,
    validate_records,
    write_jsonl,
)
from eval.public_runner import evaluate, ingest, summarize

FIXTURES = Path(__file__).resolve().parents[1] / "eval" / "datasets" / "public" / "fixtures"


# ---------------------------------------------------------------- 日期解析


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2023-05-20", date(2023, 5, 20)),
        ("2023/05/20 (Sat) 14:00", date(2023, 5, 20)),
        ("1:56 pm on 8 May, 2023", date(2023, 5, 8)),
        ("May 8, 2023", date(2023, 5, 8)),
        ("", None),
        ("not a date", None),
        ("2023-13-40", None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


# ---------------------------------------------------------------- 转换器


def test_longmemeval_evidence_and_stale():
    records = convert_longmemeval(load_raw(FIXTURES / "longmemeval.sample.json"))
    assert len(records) == 2

    single = records[0]
    assert single["source"] == "longmemeval"
    assert single["expect"] == "answer"
    assert len(single["gold_facts"]) == 1
    assert "matcha latte" in single["gold_facts"][0]["statement"]
    assert single["gold_facts"][0]["valid_from"] == "2023-05-18"
    assert single["stale_facts"] == []

    update = records[1]
    assert update["meta"]["question_type"] == "knowledge-update"
    assert any("10 km" in f["statement"] for f in update["gold_facts"])
    # 数值冲突启发式应识别出「5 km」这条旧陈述
    assert len(update["stale_facts"]) == 1
    stale = update["stale_facts"][0]
    assert "5 km" in stale["statement"]
    assert stale["valid_to"] is not None
    assert update["must_not_include"] == [stale["id"]]
    assert all(f["id"] != stale["id"] for f in update["gold_facts"])


def test_locomo_evidence_dia_id_mapping():
    records = convert_locomo(load_raw(FIXTURES / "locomo.sample.json"))
    assert len(records) == 3

    dog, temporal, adversarial = records
    assert dog["gold_facts"][0]["statement"].startswith("Caroline:")
    assert "Luna" in dog["gold_facts"][0]["statement"]
    assert dog["gold_facts"][0]["valid_from"] == "2023-06-25"

    assert temporal["meta"]["category"] == 2
    assert temporal["gold_facts"][0]["valid_from"] == "2023-05-08"

    # category 5 = 对抗样本：问题里的东西对话中没提过
    assert adversarial["expect"] == "abstain"
    assert adversarial["gold_facts"] == []


def test_hotpotqa_uses_supporting_sentences_only():
    records = convert_hotpotqa(load_raw(FIXTURES / "hotpotqa.sample.json"))
    assert len(records) == 1
    gold = records[0]["gold_facts"]
    assert len(gold) == 2  # 两条 supporting_facts，不含干扰句
    assert all("Timothy Shay Arthur" not in f["statement"] for f in gold)
    assert all(f["valid_from"] == PLACEHOLDER_DATE.isoformat() for f in gold)
    assert records[0]["meta"]["date_source"] == "placeholder"


def test_freshqa_temporal_scope_and_outdated_answers():
    records = convert_freshqa(load_raw(FIXTURES / "freshqa.sample.json"))
    assert len(records) == 2

    current = records[0]
    assert current["scope"] == "temporal"  # 无证据原文，只评时效
    assert current["gold_facts"][0]["valid_from"] == FRESHQA_CUTOFF.isoformat()
    assert len(current["stale_facts"]) == 2
    assert all(f["valid_to"] == FRESHQA_CUTOFF.isoformat() for f in current["stale_facts"])
    assert sorted(current["must_not_include"]) == sorted(f["id"] for f in current["stale_facts"])

    false_premise = records[1]
    assert false_premise["expect"] == "abstain"
    assert false_premise["gold_facts"] == []


def test_fact_ids_are_stable_and_unique():
    first = convert_locomo(load_raw(FIXTURES / "locomo.sample.json"))
    second = convert_locomo(load_raw(FIXTURES / "locomo.sample.json"))
    ids = [f["id"] for r in first for f in r["gold_facts"]]
    assert ids == [f["id"] for r in second for f in r["gold_facts"]]  # 可复现
    assert len(ids) == len(set(ids))


def test_placeholder_datasets_validate_clean():
    for name, conv in (
        ("longmemeval", convert_longmemeval),
        ("locomo", convert_locomo),
        ("hotpotqa", convert_hotpotqa),
        ("freshqa", convert_freshqa),
    ):
        records = conv(load_raw(FIXTURES / f"{name}.sample.json"))
        assert validate_records(records) == [], name


# ---------------------------------------------------------------- 校验器


def _record(question: str, facts: list, *, stale: list | None = None, **kwargs) -> dict:
    stale = stale or []
    return {
        "question": question,
        "gold_facts": facts,
        "answer": facts[0]["object"] if facts else "",
        "source": "test",
        "must_not_include": [f["id"] for f in stale],
        "stale_facts": stale,
        **kwargs,
    }


def _fact(seq: int, statement: str, valid_from: str = "2023-05-08", valid_to: str | None = None) -> dict:
    return make_fact(
        "test", "q1", seq, subject="user", predicate="fact", obj="value",
        statement=statement, valid_from=date.fromisoformat(valid_from),
        valid_to=date.fromisoformat(valid_to) if valid_to else None,
        source="dataset://test/q1",
    )


def test_validate_rejects_stale_without_valid_to():
    bad = _fact(1, "old value")
    rec = _record("现在多少？", [_fact(0, "new value")], stale=[bad])
    errors = validate_record(rec)
    assert any("valid_to" in e for e in errors)


def test_validate_rejects_banned_id_not_in_stale():
    rec = _record("现在多少？", [_fact(0, "new value")])
    rec["must_not_include"] = ["F-20230101-deadbeef"]
    assert any("没有对应的 stale_facts" in e for e in validate_record(rec))


def test_validate_rejects_abstain_with_gold():
    rec = _record("问题", [_fact(0, "x")], expect="abstain")
    assert any("不应带 gold_facts" in e for e in validate_record(rec))


def test_validate_rejects_empty_gold_when_answer_expected():
    rec = _record("问题", [])
    assert any("gold_facts 为空" in e for e in validate_record(rec))


# ---------------------------------------------------------------- 评测器


def test_ingest_writes_gold_and_stale_into_isolated_journal():
    records = convert_longmemeval(load_raw(FIXTURES / "longmemeval.sample.json"))
    with tempfile.TemporaryDirectory() as tmp:
        assert ingest(records, Path(tmp)) == 3  # 2 gold + 1 stale
        shards = list(Path(tmp).glob("*.md"))
        assert shards, "journal 分片应已落盘"


def test_stale_fact_is_recalled_at_stage0_but_filtered_at_stage3():
    """公开集的核心检验点：旧值在无时效过滤时会被召回，Stage3 必须清掉它。"""
    records = convert_longmemeval(load_raw(FIXTURES / "longmemeval.sample.json"))
    acc = evaluate(records, stages=(0, 3), top_k=5, isolate="case")
    summary = summarize(acc, 5)

    assert summary[0]["n_temporal"] == 1
    assert summary[0]["temporal_accuracy"] == pytest.approx(0.0)  # Stage0 泄露旧值
    assert summary[3]["temporal_accuracy"] == pytest.approx(1.0)  # Stage3 过滤
    assert summary[0]["hit_rate"] == pytest.approx(1.0)
    assert summary[3]["hit_rate"] == pytest.approx(1.0)


def test_isolation_case_prevents_cross_question_contamination():
    """逐题隔离：别题的事实不能挤占本题的 top_k。"""
    weak = _record("How many kilometers do I run in the morning?", [_fact(0, "I saw a morning fog.")])
    strong = _record("How many kilometers do I run in the morning?", [
        _fact(1, "I run 10 km every morning before work.")
    ])

    case_acc = evaluate([weak], stages=(0,), top_k=1, isolate="case")
    file_acc = evaluate([weak, strong], stages=(0,), top_k=1, isolate="file")
    case = summarize(case_acc, 1)[0]
    both = summarize(file_acc, 1)[0]

    assert case["recall"] == pytest.approx(1.0)  # 只灌本题事实，必然召回
    assert both["recall"] < case["recall"]  # 合并索引后本题事实被挤掉


def test_temporal_only_dataset_has_no_retrieval_metrics():
    records = convert_freshqa(load_raw(FIXTURES / "freshqa.sample.json"))
    summary = summarize(evaluate(records, stages=(0, 3), top_k=5, isolate="case"), 5)
    assert summary[0]["hit_rate"] is None  # 不参与检索聚合，渲染成「—」
    assert summary[0]["n_abstain"] == 1  # 逐 Stage 独立计数
    assert summary[0]["n_temporal"] == 1


# ---------------------------------------------------------------- CLI 端到端


def test_convert_cli_then_runner_cli(tmp_path):
    from eval import public_runner
    from eval.datasets.public import convert

    out = tmp_path / "longmemeval.gold.jsonl"
    code = convert.main([
        "--dataset", "longmemeval",
        "--input", str(FIXTURES / "longmemeval.sample.json"),
        "--output", str(out),
    ])
    assert code == 0
    assert len(read_jsonl(out)) == 2

    report = tmp_path / "report.json"
    code = public_runner.main([
        "--public-dir", str(tmp_path),
        "--datasets", "longmemeval",
        "--stages", "0,3",
        "--json", str(report),
    ])
    assert code == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["longmemeval"]["3"]["temporal_accuracy"] == pytest.approx(1.0)


def test_runner_cli_errors_when_no_dataset(tmp_path):
    from eval import public_runner

    assert public_runner.main(["--public-dir", str(tmp_path)]) == 1


def test_check_only_reports_errors(tmp_path):
    from eval.datasets.public import convert

    bad = tmp_path / "bad.gold.jsonl"
    write_jsonl([_record("问题", [])], bad)
    assert convert.main(["--check-only", str(bad)]) == 1
