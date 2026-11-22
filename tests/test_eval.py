"""T15 评测框架：指标纯函数 / 消融流程 / 对比表 / gold 加载。"""
import json

import pytest

from eval.metrics import hit_rate, mrr, recall_at_k, temporal_accuracy
from eval.runner import load_gold, run_ablation, to_markdown_table


# ---------------------------------------------------------------- 指标函数


def test_hit_rate_hit_and_miss():
    assert hit_rate(["A", "B"], {"B"}) == 1.0
    assert hit_rate(["A", "B"], {"C"}) == 0.0


def test_hit_rate_empty_results():
    assert hit_rate([], {"A"}) == 0.0


def test_hit_rate_empty_gold():
    # 无参考答案不应算满分
    assert hit_rate(["A"], set()) == 0.0


def test_hit_rate_multiple_gold_any_hit():
    assert hit_rate(["A"], {"B", "C", "A"}) == 1.0


def test_recall_at_k_partial():
    assert recall_at_k(["A", "B", "C"], {"A", "C", "D"}, 3) == pytest.approx(2 / 3)
    assert recall_at_k(["A", "B", "C"], {"A", "C", "D"}, 1) == pytest.approx(1 / 3)


def test_recall_at_k_full_and_empty():
    assert recall_at_k(["A", "B"], {"A", "B"}, 5) == 1.0
    assert recall_at_k([], {"A"}, 5) == 0.0
    assert recall_at_k(["A"], {"A"}, 0) == 0.0
    assert recall_at_k(["A"], set(), 5) == 0.0


def test_mrr_first_and_later_rank():
    assert mrr(["A", "B"], {"A"}) == 1.0
    assert mrr(["A", "B", "C"], {"C"}) == pytest.approx(1 / 3)


def test_mrr_miss_and_empty_gold():
    assert mrr(["A", "B"], {"Z"}) == 0.0
    assert mrr([], {"A"}) == 0.0
    assert mrr(["A"], set()) == 0.0


def test_temporal_accuracy_without_old_facts():
    assert temporal_accuracy(["A"], {"OLD"}) == 1.0
    assert temporal_accuracy([], {"OLD"}) == 1.0


def test_temporal_accuracy_penalises_superseded():
    assert temporal_accuracy(["A", "OLD"], {"OLD"}) == 0.0


def test_temporal_accuracy_no_trap_is_vacuous():
    # 没有旧事实陷阱时真空成立，runner 聚合时会跳过这类样本
    assert temporal_accuracy(["A"], set()) == 1.0


# ---------------------------------------------------------------- 消融流程


class FakeScored:
    """只暴露 fact_id 的检索结果替身。"""

    def __init__(self, fact_id: str):
        self.fact_id = fact_id


class StubFuse:
    """按 (stage, query) 返回固定 fact_id 列表，并记录调用参数。"""

    def __init__(self, by_stage: dict[int, dict[str, list[str]]]):
        self.by_stage = by_stage
        self.calls: list[tuple[str, int]] = []

    def recall(self, query, top_k=5, stage=3, when=None):
        self.calls.append((query, stage))
        ids = self.by_stage.get(stage, {}).get(query, [])
        return [FakeScored(i) for i in ids[:top_k]]


class FakeProvider:
    def __init__(self, score: float = 0.8):
        self.score = score
        self.calls = 0

    def judge(self, question, facts, answer):
        self.calls += 1
        return {"score": self.score}


GOLD = [
    {"question": "q1", "gold_facts": [{"id": "X"}], "answer": "a1"},
    {
        "question": "q2",
        "gold_facts": [{"id": "Y"}],
        "answer": "a2",
        "must_not_include": ["Z"],
    },
]


def test_run_ablation_structure_and_stage_gain():
    fuse = StubFuse(
        {
            0: {"q1": [], "q2": ["Z"]},
            1: {"q1": ["X"], "q2": ["Z"]},
            2: {"q1": ["X"], "q2": ["Y"]},
            3: {"q1": ["X"], "q2": ["Y"]},
        }
    )
    results = run_ablation(fuse, GOLD, stages=(0, 1, 2, 3), top_k=5)

    assert set(results) == {0, 1, 2, 3}
    for row in results.values():
        assert set(row) >= {
            "stage",
            "n",
            "hit_rate",
            "recall",
            "mrr",
            "temporal_accuracy",
            "avg_latency_ms",
            "avg_tokens",
        }
        assert row["n"] == 2
        assert row["avg_latency_ms"] >= 0.0

    # Stage0：q1 无结果、q2 只返回旧事实 Z -> 两条都未命中
    assert results[0]["hit_rate"] == 0.0
    assert results[0]["temporal_accuracy"] == 0.0
    # Stage1：q1 命中 -> hit 0.5，但 q2 仍返回旧事实
    assert results[1]["hit_rate"] == pytest.approx(0.5)
    assert results[1]["temporal_accuracy"] == 0.0
    # Stage2/3：全命中且不含旧事实
    assert results[2]["mrr"] == 1.0
    assert results[3]["hit_rate"] == 1.0
    assert results[3]["recall"] == 1.0
    assert results[3]["temporal_accuracy"] == 1.0


def test_run_ablation_passes_stage_and_respects_top_k():
    fuse = StubFuse({3: {"q1": ["X", "A", "B"], "q2": []}})
    run_ablation(fuse, GOLD[:1], stages=(3,), top_k=2)
    assert fuse.calls == [("q1", 3)]

    results = run_ablation(StubFuse({3: {"q1": ["A", "X", "B"]}}), GOLD[:1], stages=(3,), top_k=3)
    assert results[3]["recall"] == 1.0  # X 在第 2 条，k=3 覆盖
    results = run_ablation(StubFuse({3: {"q1": ["A", "X", "B"]}}), GOLD[:1], stages=(3,), top_k=1)
    assert results[3]["hit_rate"] == 0.0


def test_run_ablation_temporal_only_counts_trap_queries():
    # 只有 q2 有 must_not_include，n_temporal 应为 1
    results = run_ablation(StubFuse({3: {"q1": ["X"], "q2": ["Y"]}}), GOLD, stages=(3,))
    assert results[3]["n_temporal"] == 1
    assert results[3]["temporal_accuracy"] == 1.0


def test_run_ablation_judge_optional():
    fuse = StubFuse({3: {"q1": ["X"], "q2": ["Y"]}})
    no_judge = run_ablation(fuse, GOLD, stages=(3,))
    assert no_judge[3]["avg_judge_score"] is None
    assert no_judge[3]["n_judged"] == 0

    provider = FakeProvider(0.75)
    with_judge = run_ablation(fuse, GOLD, stages=(3,), provider=provider)
    assert provider.calls == 2
    assert with_judge[3]["avg_judge_score"] == pytest.approx(0.75)


def test_run_ablation_judge_failure_is_not_fatal():
    class BrokenProvider:
        def judge(self, question, facts, answer):
            raise RuntimeError("model down")

    results = run_ablation(StubFuse({3: {"q1": ["X"], "q2": ["Y"]}}), GOLD, stages=(3,), provider=BrokenProvider())
    assert results[3]["avg_judge_score"] is None
    assert results[3]["hit_rate"] == 1.0


# ---------------------------------------------------------------- 对比表


def test_to_markdown_table_has_header_and_rows():
    results = run_ablation(StubFuse({0: {"q1": ["X"], "q2": ["Y"]}, 3: {"q1": ["X"], "q2": ["Y"]}}), GOLD, stages=(0, 3))
    table = to_markdown_table(results)
    lines = table.splitlines()

    assert lines[0] == "| Stage | HitRate | Recall@5 | MRR | 时效正确率 | 平均延迟(ms) |"
    assert len(lines) == 2 + 2  # 表头 + 分隔 + 2 行
    assert lines[2].startswith("| 0 · BM25 |")
    assert lines[3].startswith("| 3 · +时效 |")
    assert "Judge" not in table  # 无 judge 分数时不显示该列


def test_to_markdown_table_shows_judge_column_when_available():
    fuse = StubFuse({3: {"q1": ["X"], "q2": ["Y"]}})
    results = run_ablation(fuse, GOLD, stages=(3,), provider=FakeProvider(0.9))
    table = to_markdown_table(results)
    assert "| Judge |" in table
    assert "0.900" in table


def test_to_markdown_table_empty():
    assert to_markdown_table({}) == "（无结果）"


# ---------------------------------------------------------------- gold 加载


def test_load_gold_skips_blank_lines(tmp_path):
    path = tmp_path / "gold.jsonl"
    rows = [
        {"question": "q1", "gold_facts": [{"id": "A"}], "answer": "a1"},
        {"question": "q2", "gold_facts": [], "answer": "a2", "must_not_include": ["OLD"]},
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n\n", encoding="utf-8")

    gold = load_gold(path)
    assert len(gold) == 2
    assert gold[0]["question"] == "q1"
    assert gold[1]["must_not_include"] == ["OLD"]
