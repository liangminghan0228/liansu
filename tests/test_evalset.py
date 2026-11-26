"""T14 评测集格式自检。

只校验评测集自身的格式自洽，不校验 gold_facts 的 vault:// 真源是否真实存在
（vault 尚未落地，source 为约定前缀，待真源落地后再补"存在性校验"）。
"""
from pathlib import Path

import pytest

GOLD = Path(__file__).resolve().parents[1] / "eval" / "datasets" / "own" / "gold.jsonl"

# 每条 gold fact 必须携带的最小字段（对齐 liansu.schema.Fact 的必填字段）
FACT_FIELDS = {
    "id": str,
    "subject": str,
    "predicate": str,
    "object": str,
    "statement": str,
    "valid_from": str,
    "confidence": (int, float),
    "topics": list,
    "source": str,
}
RECORD_FIELDS = {"question", "gold_facts", "answer", "source"}


@pytest.fixture(scope="module")
def records():
    import json

    lines = GOLD.read_text(encoding="utf-8").splitlines()
    out = []
    for no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            pytest.fail(f"gold.jsonl 第 {no} 行不是合法 JSON: {exc}")
    return out


def test_gold_not_empty(records):
    assert len(records) >= 20, f"自建集至少 20 条 question，实际 {len(records)} 条"
    assert len(records) <= 30, f"自建集建议不超过 30 条 question，实际 {len(records)} 条"


@pytest.mark.parametrize("field", sorted(RECORD_FIELDS))
def test_record_has_all_fields(records, field):
    for i, rec in enumerate(records):
        assert field in rec, f"第 {i + 1} 条记录缺少字段 {field}"


def test_question_and_answer_nonempty(records):
    for i, rec in enumerate(records):
        assert isinstance(rec["question"], str) and rec["question"].strip(), (
            f"第 {i + 1} 条 question 为空"
        )
        assert isinstance(rec["answer"], str) and rec["answer"].strip(), (
            f"第 {i + 1} 条 answer 为空"
        )


def test_gold_facts_nonempty_and_schema(records):
    for i, rec in enumerate(records):
        facts = rec["gold_facts"]
        assert isinstance(facts, list) and facts, f"第 {i + 1} 条 gold_facts 为空"
        for j, fact in enumerate(facts):
            assert isinstance(fact, dict), f"第 {i + 1} 条第 {j + 1} 个 fact 不是对象"
            for field, expected_type in FACT_FIELDS.items():
                assert field in fact, (
                    f"第 {i + 1} 条第 {j + 1} 个 fact 缺少字段 {field}"
                )
                assert isinstance(fact[field], expected_type), (
                    f"第 {i + 1} 条第 {j + 1} 个 fact 字段 {field} 类型不符"
                )
            assert 0.0 <= fact["confidence"] <= 1.0, (
                f"第 {i + 1} 条第 {j + 1} 个 fact confidence 越界"
            )
            assert all(isinstance(t, str) for t in fact["topics"]), (
                f"第 {i + 1} 条第 {j + 1} 个 fact topics 元素非字符串"
            )
            assert fact["source"].startswith("vault://"), (
                f"第 {i + 1} 条第 {j + 1} 个 fact source 应使用 vault:// 约定前缀"
            )


def test_temporal_samples_at_least_ten(records):
    """时效性样本以 must_not_include 字段标识。"""
    temporal = [r for r in records if r.get("must_not_include")]
    assert len(temporal) >= 10, (
        f"时效性样本至少 10 条，实际 {len(temporal)} 条"
    )
    for rec in temporal:
        assert isinstance(rec["must_not_include"], list) and rec["must_not_include"]
        # 被排除的旧 fact id 不能同时出现在应命中的 gold_facts 里
        gold_ids = {f["id"] for f in rec["gold_facts"]}
        for old_id in rec["must_not_include"]:
            assert old_id not in gold_ids, (
                f"旧事实 {old_id} 不应出现在 gold_facts 中"
            )


def test_fact_ids_unique_within_record(records):
    for i, rec in enumerate(records):
        ids = [f["id"] for f in rec["gold_facts"]]
        assert len(ids) == len(set(ids)), f"第 {i + 1} 条 gold_facts 存在重复 id"
