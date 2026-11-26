"""端到端评测用的 stub provider。

只做一件事：按对话内容查表返回**预置抽取结果**，让「record → 落盘 → supersede →
建索引 → recall → 指标」这条链路在**不依赖网络和模型**的前提下可被单测和冒烟跑通。

用途边界（重要）：
- stub 的抽取结果来自用例里的理想标注，所以 stub 通道的抽取指标天然接近满分，
  **只能证明评测链路本身没写错，不能当模型能力结论**；
- 真实抽取质量必须用 ``--provider real`` 走 ``ApiProvider`` 跑。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from liansu.providers.base import LLMProvider
from liansu.schema import Fact


def dialog_key(dialog: list[dict[str, str]]) -> str:
    """对话内容指纹；不掺 source，保证 pre_dialog / dialog 可分别查表。"""
    payload = json.dumps(dialog, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FixtureProvider(LLMProvider):
    """table: 对话指纹 → fact 规格列表；gates: 对话指纹 → 是否值得记。"""

    def __init__(
        self,
        table: dict[str, list[dict[str, Any]]],
        gates: dict[str, bool] | None = None,
        confidence: float = 0.9,
    ):
        self.table = table
        self.gates = gates or {}
        self.confidence = confidence
        self._seq = 0

    def gate(self, dialog: list[dict[str, str]]) -> bool:
        return self.gates.get(dialog_key(dialog), True)

    def extract(self, dialog: list[dict[str, str]], source: str = "") -> list[Fact]:
        specs = self.table.get(dialog_key(dialog), [])
        facts: list[Fact] = []
        for spec in specs:
            self._seq += 1
            data = {
                "id": spec.get("id") or f"F-{date.today():%Y%m%d}-{self._seq:04d}",
                "subject": spec["subject"],
                "predicate": spec["predicate"],
                "object": spec["object"],
                "statement": spec.get("statement") or f"{spec['subject']} {spec['predicate']} {spec['object']}",
                "valid_from": spec.get("valid_from") or date.today().isoformat(),
                "confidence": spec.get("confidence", self.confidence),
                "topics": spec.get("topics", []),
                "source": source or spec.get("source", "stub"),
            }
            facts.append(Fact.from_dict(data))
        return facts

    def judge(self, question: str, facts: list[Fact], answer: str) -> dict:
        raise NotImplementedError("stub 通道不提供 judge，请用 --provider real")
