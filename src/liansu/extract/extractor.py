"""T4 · 结构化抽取 + confidence 门控。"""
from __future__ import annotations

from liansu.providers.base import LLMProvider
from liansu.schema import Fact

DEFAULT_CONFIDENCE_THRESHOLD = 0.7


def extract_facts(
    provider: LLMProvider,
    dialog: list[dict[str, str]],
    source: str = "",
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[Fact]:
    """调 provider 抽取并过滤低置信事实；非法/缺失字段的 fact 直接丢弃而非整体失败。"""
    raw = provider.extract(dialog, source=source)
    kept: list[Fact] = []
    seen: set[tuple[str, str]] = set()
    for f in raw:
        try:
            fact = f if isinstance(f, Fact) else Fact.from_dict(f)
        except Exception:
            continue
        if fact.confidence < confidence_threshold:
            continue
        key = (fact.subject, fact.predicate)
        if key in seen:          # 同批次内同 key 只留置信度最高的
            continue
        seen.add(key)
        kept.append(fact)
    return kept
