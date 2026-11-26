"""T15 · 端到端评测的语义匹配层。

与 ``eval/metrics.py`` 的分工：
- ``metrics.py``：给定 fact_id 序列算 HitRate / Recall / MRR / 时效（判分）；
- 本模块：解决「抽取出的 fact 怎么和 gold 对上」——抽取出的 id 由系统生成
  （``F-{日期}-{uuid8}``，模型无权写主键），评测不可能按 id 比对，只能按语义。

匹配分三档（归一化后比较三元组）：

- **strict**：subject / predicate / object 全等 —— 谓词也写对了，
  ``Journal.find_by_key(subject, predicate)`` 才能命中，supersede 才会触发；
- **loose**：subject + object 相等、predicate 可不同 —— 容忍谓词漂移
  （E2E_REPORT 里 ``用户|在...之间还没定下来|北京和西安`` 这类）。
  语义抽对了但谓词漂移，检索侧命中、写侧合并失败，必须分开统计才能看出来；
- **None**：不匹配。

归一化只做保守处理（NFKC、大小写、空白标点、少量量词同义），不做同义词推理，
避免把「北京」和「上海」判成等价。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Sequence

__all__ = [
    "normalize",
    "fact_key",
    "spec_key",
    "match_spec",
    "match_any",
    "match_banned",
    "score_extraction",
]

# 量词/单位同义：只在归一化阶段替换，保持保守（不碰方位、城市、人名）
_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("公斤", "kg"),
    ("千克", "kg"),
    ("厘米", "cm"),
    ("公分", "cm"),
    ("毫米", "mm"),
    ("小时", "h"),
    ("分钟", "min"),
)

# 只保留：数字、拉丁字母、CJK 汉字、小数点（保留小数，避免 82.5 与 825 误判相等）
_KEEP = re.compile(r"[^0-9a-z\u4e00-\u9fff.]+")


def normalize(text: Any) -> str:
    """归一化：NFKC → 小写 → 只留数字/字母/汉字/小数点 → 量词同义。"""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).casefold()
    s = _KEEP.sub("", s)
    for old, new in _SYNONYMS:
        s = s.replace(old, new)
    return s


def fact_key(fact: Any) -> tuple[str, str, str]:
    """Fact 或 dict → 归一化三元组。"""
    get = (lambda k: getattr(fact, k, None)) if not isinstance(fact, dict) else fact.get
    return (normalize(get("subject")), normalize(get("predicate")), normalize(get("object")))


def spec_key(spec: dict) -> tuple[str, str, str]:
    return (normalize(spec.get("subject")), normalize(spec.get("predicate")), normalize(spec.get("object")))


def match_spec(fact: Any, spec: dict) -> str | None:
    """单条比对，返回 ``"strict"`` / ``"loose"`` / ``None``。"""
    fk, sk = fact_key(fact), spec_key(spec)
    if fk == sk:
        return "strict"
    if fk[0] == sk[0] and fk[2] == sk[2]:   # 主语 + 客体一致，谓词漂移
        return "loose"
    return None


def match_any(fact: Any, specs: Sequence[dict]) -> tuple[int | None, str | None]:
    """在 specs 里找第一条命中的，返回 ``(下标, 匹配档位)``。"""
    best: tuple[int | None, str | None] = (None, None)
    for i, spec in enumerate(specs):
        mode = match_spec(fact, spec)
        if mode == "strict":
            return i, "strict"
        if mode == "loose" and best[0] is None:
            best = (i, "loose")
    return best


def match_banned(facts: Iterable[Any], banned_specs: Sequence[dict]) -> list[Any]:
    """返回命中了「旧事实」规格的 fact 列表（用于时效泄漏判定）。"""
    hits: list[Any] = []
    for fact in facts:
        if match_any(fact, banned_specs)[0] is not None:
            hits.append(fact)
    return hits


def _f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if (p + r) else 0.0


def score_extraction(predicted: Sequence[Any], gold_specs: Sequence[dict]) -> dict:
    """抽取质量：以 gold 规格为基准算 precision / recall / F1（一对一贪心匹配）。

    边界约定：
    - 双方都为空（该记但确实没什么可记 / 不该记）→ P = R = F1 = 1.0；
    - 只一方为空 → 对应指标 0.0，不把「没抽」算成满分；
    - ``predicate_match_rate`` = strict 命中 / 总命中，衡量谓词规范化程度，
      无命中时为 ``None``（无信息量，不参与聚合）。
    """
    gold = list(gold_specs)
    used: set[int] = set()
    tp = strict = 0
    matched_pred: set[int] = set()

    for pi, fact in enumerate(predicted):
        idx, mode = match_any(fact, gold)
        if idx is None or idx in used:
            continue
        used.add(idx)
        matched_pred.add(pi)
        tp += 1
        if mode == "strict":
            strict += 1

    n_pred, n_gold = len(predicted), len(gold)
    if n_pred == 0 and n_gold == 0:
        precision = recall = 1.0
    else:
        precision = tp / n_pred if n_pred else 0.0
        recall = tp / n_gold if n_gold else 0.0

    return {
        "n_predicted": n_pred,
        "n_gold": n_gold,
        "tp": tp,
        "strict_tp": strict,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "predicate_match_rate": (strict / tp) if tp else None,
        "missed": [gold[i] for i in range(n_gold) if i not in used],
        "extra": [predicted[i] for i in range(n_pred) if i not in matched_pred],
    }
