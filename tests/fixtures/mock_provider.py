"""Mock provider — 并行期替身，真实 ApiProvider 到位后只换注入不改上层。"""
from __future__ import annotations

from liansu.providers.base import LLMProvider
from liansu.schema import Fact


class MockProvider(LLMProvider):
    """可配置行为的 mock：gate 返回值 / extract 产出可由构造参数决定。"""

    def __init__(self, gate: bool = True, facts: list[Fact] | None = None, judge_score: float = 0.9):
        self.gate_result = gate
        self.facts = facts or []
        self.judge_score = judge_score
        self.calls: list[str] = []

    def gate(self, dialog: list[dict[str, str]]) -> bool:
        self.calls.append("gate")
        return self.gate_result

    def extract(self, dialog: list[dict[str, str]], source: str = "") -> list[Fact]:
        self.calls.append("extract")
        out = []
        for f in self.facts:
            data = f.to_dict()
            if source and not data.get("source"):
                data["source"] = source
            out.append(Fact.from_dict(data))
        return out

    def judge(self, question: str, facts: list[Fact], answer: str) -> dict:
        self.calls.append("judge")
        return {"score": self.judge_score, "reason": "mock"}
