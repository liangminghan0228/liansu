"""公开数据集 → 统一评测格式（gold JSONL）的转换器。

分工：
- ``eval.runner``：假定 fact 已在 vault，直接检索打分；
- ``eval.public_runner``：公开集 fact 只存在于数据集里，先把 evidence 灌进隔离 journal 再召回；
- 本模块：只做「原始格式 → 统一格式」，不做检索、不写 journal，便于单测与离线复现。

统一格式在 ``docs/evalset.md`` §3 基础上向后兼容地加四个可选字段：

============ ================================================================
字段          含义
============ ================================================================
gold_facts    应命中的 fact（对象数组），statement 一律填证据原文
must_not_include 旧 fact id，仅用于时效正确率判分
stale_facts   旧 fact 的完整对象（``valid_to`` 必填），灌库后由 Stage3 过滤
expect        ``answer``（默认）｜``abstain``（对抗样本，不计入检索指标）
scope         ``retrieval``（默认）｜``temporal``（无证据原文，只评时效）
meta         数据集内的溯源信息（qid / category / 日期 / 证据 id），不参与判分
============ ================================================================

三条口径约定（同时写进 docs/evalset.md）：

1. **公开集只评「检索 + 时效」，不评抽取**。原始数据没有
   ``(subject, predicate, object)`` 标注，机械三元组会把抽取指标变成噪声；
   谓词填数据集自带的类型标签，仅作溯源。
2. **statement 是证据原文**，索引与 BM25 只吃这段文字；``object`` 取数据集答案，
   仅方便人读。没有证据原文的数据集（FreshQA）只把答案写成 statement，
   并把 ``scope`` 标成 ``temporal``，避免用问题原文当证据造成「自我命中」。
3. **时效靠 stale_facts 显式给出**：知识更新题取更新日期，FreshQA 取统一截断日，
   无日期的数据集（HotpotQA）用 ``PLACEHOLDER_DATE`` 并在 ``meta`` 标注。

用法::

    .venv/bin/python -m eval.datasets.public.convert --dataset longmemeval \\
        --input raw/longmemeval_s.json --output eval/datasets/public/longmemeval.gold.jsonl
    .venv/bin/python -m eval.datasets.public.convert --dataset all \\
        --input-dir raw/ --output-dir eval/datasets/public
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:  # 允许直接 python convert.py，无需先 pip install
    sys.path.insert(0, str(_SRC))

from liansu.schema import Fact  # noqa: E402

# ---------------------------------------------------------------- 常量

DATASETS: tuple[str, ...] = ("longmemeval", "locomo", "hotpotqa", "freshqa")

#: 数据集没有真实日期时的占位（HotpotQA 的维基条目无时间维度）。
PLACEHOLDER_DATE = date(2000, 1, 1)

#: FreshQA 没有逐条答案日期，用统一截断日区分「旧答案 / 现行答案」。
FRESHQA_CUTOFF = date(2025, 1, 1)

#: 单条记录最多保留的 gold fact 数，避免长程数据集一条题塞进上百条证据。
MAX_FACTS_PER_RECORD = 8

_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_SLASH_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
_DMY_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9}),?\s+(\d{4})")
_MDY_RE = re.compile(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

_STOPWORDS: frozenset[str] = frozenset(
    """a an the i me my we our you your it its this that these those is are was were be
    been being am do does did have has had to of in on at for with and or but not no so
    than then there here now just also very""".split()
)


# ---------------------------------------------------------------- 基础工具


def parse_date(text: Any) -> date | None:
    """解析数据集里的各种日期写法，解析不出返回 None。

    覆盖：``2023-05-20``、``2023/05/20 (Sat) 02:21``、``1:56 pm on 8 May, 2023``、
    ``May 8, 2023``。
    """
    if isinstance(text, date):
        return text
    if not isinstance(text, str) or not text.strip():
        return None
    for pattern, order in ((_ISO_RE, "ymd"), (_SLASH_RE, "ymd")):
        m = pattern.search(text)
        if m:
            y, mo, d = (int(x) for x in m.groups())
            try:
                return date(y, mo, d)
            except ValueError:
                return None
    m = _DMY_RE.search(text)
    if m:
        d, mon, y = m.groups()
        month = _MONTHS.get(mon.lower())
        if month:
            try:
                return date(int(y), month, int(d))
            except ValueError:
                return None
    m = _MDY_RE.search(text)
    if m:
        mon, d, y = m.groups()
        month = _MONTHS.get(mon.lower())
        if month:
            try:
                return date(int(y), month, int(d))
            except ValueError:
                return None
    return None


def _clean(text: Any) -> str:
    """折叠空白，去掉首尾空行 —— journal 里一条 fact 必须是一行文本。"""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _fact_id(dataset: str, qid: str, seq: int, valid_from: date) -> str:
    """稳定 id：同一 (dataset, qid, 序号) 永远得到同一个 id，便于 diff 与复现。"""
    digest = hashlib.sha1(f"{dataset}|{qid}|{seq}".encode("utf-8")).hexdigest()[:8]
    return f"F-{valid_from:%Y%m%d}-{digest}"


def make_fact(
    dataset: str,
    qid: str,
    seq: int,
    *,
    subject: str,
    predicate: str,
    obj: str,
    statement: str,
    valid_from: date,
    source: str,
    valid_to: date | None = None,
    topics: Iterable[str] = (),
) -> dict[str, Any]:
    """构造一条 gold fact dict，并经 ``Fact`` 校验；不写 ``created_at`` 保证可复现。"""
    payload = {
        "id": _fact_id(dataset, qid, seq, valid_from),
        "subject": _clean(subject) or "unknown",
        "predicate": _clean(predicate) or "unknown",
        "object": _clean(obj) or "unknown",
        "statement": _clean(statement),
        "valid_from": valid_from.isoformat(),
        "valid_to": valid_to.isoformat() if valid_to else None,
        "confidence": 0.9,  # 公开集无置信标注，统一填文档约定的 0.9
        "topics": [t for t in topics if t],
        "source": source,
    }
    Fact.from_dict(payload)  # 字段/类型/时间窗校验，坏数据在转换期就炸
    return payload


def _make_record(
    *,
    question: str,
    gold: Sequence[dict],
    answer: str,
    dataset: str,
    meta: dict[str, Any],
    stale: Sequence[dict] = (),
    expect: str = "answer",
    scope: str = "retrieval",
) -> dict[str, Any]:
    """组装统一格式的一条记录。"""
    if expect not in ("answer", "abstain"):
        raise ValueError(f"expect 只能是 answer/abstain，收到 {expect!r}")
    if scope not in ("retrieval", "temporal"):
        raise ValueError(f"scope 只能是 retrieval/temporal，收到 {scope!r}")
    if expect == "abstain" and gold:
        raise ValueError("对抗样本（abstain）不应带 gold_facts")
    return {
        "question": _clean(question),
        "gold_facts": list(gold),
        "answer": _clean(answer),
        "source": dataset,
        "must_not_include": [f["id"] for f in stale],
        "stale_facts": list(stale),
        "expect": expect,
        "scope": scope,
        "meta": meta,
    }


# ---------------------------------------------------------------- LongMemEval


def _content_words(text: str) -> set[str]:
    """小写化内容词，供中文以外（LongMemEval 为英文）的近似同主题判断。"""
    return {
        w for w in re.findall(r"[a-z0-9]+", text.lower())
        if w not in _STOPWORDS and not w.isdigit()
    }


def _knowledge_update_stale(
    dataset: str, qid: str, all_turns: list[tuple[date, str]], gold_fact: dict[str, Any],
) -> list[dict[str, Any]]:
    """知识更新题的旧值探测（保守启发式）。

    规则：gold 证据句里出现数字 N，则同主题（≥3 个内容词重叠）且更早、
    数字不同的用户陈述视为旧值。覆盖不了「改城市/换公司」这类非数字更新，
    这类题在公开集里没有旧值标注，宁可少标也不误标（误标会污染时效指标分母）。
    """
    gold_numbers = set(_NUMBER_RE.findall(gold_fact["statement"]))
    if not gold_numbers:
        return []
    gold_words = _content_words(gold_fact["statement"])
    gold_date = date.fromisoformat(gold_fact["valid_from"])
    stale: list[dict[str, Any]] = []
    for turn_date, text in all_turns:
        if turn_date > gold_date or text == gold_fact["statement"]:
            continue
        numbers = set(_NUMBER_RE.findall(text))
        if not numbers or numbers & gold_numbers:
            continue
        if len(gold_words & _content_words(text)) < 3:
            continue
        stale.append(
            make_fact(
                dataset, f"{qid}-stale{len(stale)}", len(stale),
                subject=gold_fact["subject"], predicate=gold_fact["predicate"],
                obj="previous", statement=text, valid_from=turn_date,
                valid_to=max(gold_date, turn_date),
                source=f"dataset://{dataset}/{qid}#stale{len(stale)}",
                topics=["stale"],
            )
        )
        if len(stale) >= 3:
            break
    return stale


def convert_longmemeval(raw: Any) -> list[dict[str, Any]]:
    """LongMemEval（``longmemeval_s.json``）→ 统一格式。

    题面 ``answer_session_ids`` 指向证据会话，证据会话里的用户陈述即 gold fact；
    ``question_type == knowledge-update`` 额外用数值冲突启发式补 ``stale_facts``。
    """
    items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("instances") or [])
    out: list[dict[str, Any]] = []
    for it in items:
        qid = str(it.get("question_id") or it.get("id") or len(out))
        qtype = str(it.get("question_type") or "memory")
        answer = _clean(it.get("answer"))
        sessions = it.get("haystack_sessions") or []
        dates = it.get("haystack_dates") or []
        sids = it.get("haystack_session_ids") or [f"s{i}" for i in range(len(sessions))]
        answer_sids = {str(s) for s in (it.get("answer_session_ids") or [])}

        all_turns: list[tuple[date, str]] = []
        for k, sess in enumerate(sessions):
            when = parse_date(dates[k]) if k < len(dates) else None
            when = when or parse_date(it.get("question_date")) or PLACEHOLDER_DATE
            for turn in sess or []:
                if turn.get("role") not in (None, "user"):
                    continue
                text = _clean(turn.get("content"))
                if text:
                    all_turns.append((when, text))

        evidence: list[tuple[date, str]] = []
        for k, sess in enumerate(sessions):
            if answer_sids and str(sids[k] if k < len(sids) else f"s{k}") not in answer_sids:
                continue
            when = parse_date(dates[k]) if k < len(dates) else None
            when = when or parse_date(it.get("question_date")) or PLACEHOLDER_DATE
            for turn in sess or []:
                if turn.get("role") not in (None, "user"):
                    continue
                text = _clean(turn.get("content"))
                if text:
                    evidence.append((when, text))
        if not evidence:  # 证据会话标注缺失时退化为「含答案词的陈述」
            hits = [t for t in all_turns if answer and answer.lower() in t[1].lower()]
            evidence = hits or all_turns

        gold = [
            make_fact(
                "longmemeval", qid, i,
                subject="user", predicate=qtype, obj=answer,
                statement=text, valid_from=when,
                source=f"dataset://longmemeval/{qid}#turn{i}",
                topics=[qtype],
            )
            for i, (when, text) in enumerate(evidence[:MAX_FACTS_PER_RECORD])
        ]
        stale = (
            _knowledge_update_stale("longmemeval", qid, all_turns, gold[0])
            if qtype == "knowledge-update" and gold else []
        )
        out.append(
            _make_record(
                question=str(it.get("question") or ""),
                gold=gold, answer=answer, dataset="longmemeval",
                stale=stale,
                meta={
                    "qid": qid, "question_type": qtype,
                    "question_date": (parse_date(it.get("question_date")) or PLACEHOLDER_DATE).isoformat(),
                    "evidence_sessions": sorted(answer_sids),
                },
            )
        )
    return out


# ---------------------------------------------------------------- LoCoMo


def _locomo_turns(conv: dict[str, Any]) -> tuple[dict[str, tuple[str, str, date]], dict[str, date]]:
    """展开 ``session_N`` / ``session_N_date_time``，返回 dia_id→(speaker,text,date) 与 session→date。"""
    turns: dict[str, tuple[str, str, date]] = {}
    session_dates: dict[str, date] = {}
    for key in conv:
        m = re.fullmatch(r"session_(\d+)", key)
        if not m:
            continue
        no = m.group(1)
        turns_raw = conv.get(key) or []
        when = parse_date(conv.get(f"session_{no}_date_time")) or PLACEHOLDER_DATE
        session_dates[f"D{no}"] = when
        for i, turn in enumerate(turns_raw, start=1):
            if not isinstance(turn, dict):
                continue
            speaker = str(turn.get("speaker") or "speaker")
            text = _clean(turn.get("text") or turn.get("content"))
            if not text:
                continue
            dia = str(turn.get("dia_id") or f"D{no}:{i}")
            turns[dia] = (speaker, text, when)
    return turns, session_dates


def convert_locomo(raw: Any) -> list[dict[str, Any]]:
    """LoCoMo（``locomo10.json``）→ 统一格式。

    每条 qa 的 ``evidence`` 是 dia_id 列表（``D1:3``），正好是天然 gold 标注；
    ``category == 5`` 为对抗样本（答案 Not mentioned），转成 ``expect=abstain``。
    """
    samples = raw if isinstance(raw, list) else (raw.get("samples") or raw.get("data") or [])
    out: list[dict[str, Any]] = []
    for sample in samples:
        sid = str(sample.get("sample_id") or sample.get("id") or len(out))
        conv = sample.get("conversation") or {}
        conv = conv if isinstance(conv, dict) else {}
        turns, _ = _locomo_turns(conv)
        for j, qa in enumerate(sample.get("qa") or []):
            qid = f"{sid}-qa{j}"
            category = qa.get("category")
            answer = _clean(qa.get("answer"))
            expect = "abstain" if category == 5 else "answer"
            gold: list[dict[str, Any]] = []
            for k, dia in enumerate(qa.get("evidence") or []):
                hit = turns.get(str(dia))
                if not hit:
                    continue
                speaker, text, when = hit
                gold.append(
                    make_fact(
                        "locomo", qid, k,
                        subject=speaker, predicate=f"category_{category}", obj=answer,
                        statement=f"{speaker}: {text}", valid_from=when,
                        source=f"dataset://locomo/{sid}/{dia}",
                        topics=[f"category_{category}"],
                    )
                )
            out.append(
                _make_record(
                    question=str(qa.get("question") or ""),
                    gold=gold[:MAX_FACTS_PER_RECORD], answer=answer, dataset="locomo",
                    expect=expect,
                    meta={
                        "qid": qid, "category": category,
                        "evidence": list(qa.get("evidence") or []),
                    },
                )
            )
    return out


# ---------------------------------------------------------------- HotpotQA


def convert_hotpotqa(raw: Any) -> list[dict[str, Any]]:
    """HotpotQA（``hotpot_*_v1.1.json``）→ 统一格式。

    ``supporting_facts`` 是 (标题, 句号)，对应 ``context`` 里的支撑句 —— 天然 gold。
    HotpotQA 无时间维度，``valid_from`` 统一用 ``PLACEHOLDER_DATE``。
    """
    items = raw if isinstance(raw, list) else (raw.get("data") or [])
    out: list[dict[str, Any]] = []
    for it in items:
        qid = str(it.get("_id") or it.get("id") or len(out))
        answer = _clean(it.get("answer"))
        context: dict[str, list[str]] = {}
        for para in it.get("context") or []:
            if isinstance(para, dict):
                title = str(para.get("title") or "")
                context[title] = [_clean(s) for s in (para.get("sentences") or para.get("text") or [])]
            elif isinstance(para, (list, tuple)) and len(para) >= 2:
                context[str(para[0])] = [_clean(s) for s in (para[1] or [])]
        gold: list[dict[str, Any]] = []
        for sf in it.get("supporting_facts") or []:
            if isinstance(sf, dict):
                title, idx = str(sf.get("title") or ""), int(sf.get("sent_id", 0))
            elif isinstance(sf, (list, tuple)) and len(sf) >= 2:
                title, idx = str(sf[0]), int(sf[1])
            else:
                continue
            sentences = context.get(title) or []
            if 0 <= idx < len(sentences) and sentences[idx]:
                text = sentences[idx]
                gold.append(
                    make_fact(
                        "hotpotqa", qid, len(gold),
                        subject=title, predicate=str(it.get("type") or "fact"), obj=answer,
                        statement=text, valid_from=PLACEHOLDER_DATE,
                        source=f"dataset://hotpotqa/{qid}/{title}#s{idx}",
                        topics=[title],
                    )
                )
        out.append(
            _make_record(
                question=str(it.get("question") or ""),
                gold=gold[:MAX_FACTS_PER_RECORD], answer=answer, dataset="hotpotqa",
                meta={
                    "qid": qid, "type": it.get("type"),
                    "date_source": "placeholder", "valid_from": PLACEHOLDER_DATE.isoformat(),
                },
            )
        )
    return out


# ---------------------------------------------------------------- FreshQA


def convert_freshqa(raw: Any) -> list[dict[str, Any]]:
    """FreshQA（``freshqa_v*.json`` 等）→ 统一格式，只评时效。

    没有证据原文，``answer`` 就是全部文本，所以 ``scope=temporal``：不参与 HitRate
    聚合，只统计「旧答案会不会被召回」。``false_premise=True`` 转对抗样本。
    """
    items = raw if isinstance(raw, list) else (raw.get("data") or [])
    out: list[dict[str, Any]] = []
    for it in items:
        qid = str(it.get("question_id") or it.get("id") or len(out))
        question = str(it.get("question") or "")
        answers = it.get("answer")
        if isinstance(answers, list):
            answers = answers[0] if answers else ""
        answer = _clean(answers)
        outdated = it.get("outdated_answers") or it.get("old_answers") or []
        if isinstance(outdated, str):
            outdated = [outdated]
        expect = "abstain" if it.get("false_premise") else "answer"
        gold = [] if expect == "abstain" else [
            make_fact(
                "freshqa", qid, 0,
                subject="world", predicate=str(it.get("fact_type") or "fact"), obj=answer,
                statement=answer, valid_from=FRESHQA_CUTOFF,
                source=f"dataset://freshqa/{qid}#current",
                topics=["fresh"],
            )
        ]
        stale = [
            make_fact(
                "freshqa", f"{qid}-stale{k}", k,
                subject="world", predicate=str(it.get("fact_type") or "fact"), obj=_clean(old),
                statement=_clean(old), valid_from=FRESHQA_CUTOFF - timedelta(days=365),
                valid_to=FRESHQA_CUTOFF,
                source=f"dataset://freshqa/{qid}#outdated{k}",
                topics=["stale"],
            )
            for k, old in enumerate(outdated[:3]) if _clean(old)
        ]
        out.append(
            _make_record(
                question=question, gold=gold, answer=answer, dataset="freshqa",
                stale=stale, expect=expect, scope="temporal",
                meta={
                    "qid": qid, "fact_type": it.get("fact_type"),
                    "false_premise": bool(it.get("false_premise")),
                    "cutoff": FRESHQA_CUTOFF.isoformat(),
                },
            )
        )
    return out


CONVERTERS = {
    "longmemeval": convert_longmemeval,
    "locomo": convert_locomo,
    "hotpotqa": convert_hotpotqa,
    "freshqa": convert_freshqa,
}


# ---------------------------------------------------------------- 校验


def validate_record(rec: dict[str, Any], where: str = "") -> list[str]:
    """校验一条统一格式记录，返回错误列表（空列表 = 通过）。"""
    errors: list[str] = []
    prefix = f"{where}: " if where else ""

    for field in ("question", "gold_facts", "answer", "source"):
        if field not in rec:
            errors.append(f"{prefix}缺少字段 {field}")
    if errors:
        return errors

    if not str(rec["question"]).strip():
        errors.append(f"{prefix}question 为空")
    if not str(rec["answer"]).strip():
        errors.append(f"{prefix}answer 为空")

    gold = rec["gold_facts"]
    if not isinstance(gold, list):
        return errors + [f"{prefix}gold_facts 不是数组"]
    if not gold and rec.get("expect", "answer") != "abstain":
        errors.append(f"{prefix}gold_facts 为空且 expect != abstain")

    ids: set[str] = set()
    for i, fact in enumerate(gold):
        try:
            f = Fact.from_dict(fact)
        except Exception as exc:  # pydantic 校验信息足够定位
            errors.append(f"{prefix}gold_facts[{i}] 不是合法 Fact: {exc}")
            continue
        if f.id in ids:
            errors.append(f"{prefix}gold_facts[{i}] id 重复: {f.id}")
        ids.add(f.id)

    stale = rec.get("stale_facts") or []
    for i, fact in enumerate(stale):
        try:
            f = Fact.from_dict(fact)
        except Exception as exc:
            errors.append(f"{prefix}stale_facts[{i}] 不是合法 Fact: {exc}")
            continue
        if f.valid_to is None:
            errors.append(f"{prefix}stale_facts[{i}] 缺 valid_to（否则 Stage3 过滤不掉）")
        if f.id in ids:
            errors.append(f"{prefix}stale_facts[{i}] id 与 gold 重复: {f.id}")

    banned = rec.get("must_not_include") or []
    stale_ids = {f.get("id") for f in stale}
    for bid in banned:
        if bid not in stale_ids:
            errors.append(f"{prefix}must_not_include 里的 {bid} 没有对应的 stale_facts")
        if bid in ids:
            errors.append(f"{prefix}旧事实 {bid} 不应出现在 gold_facts 中")

    if rec.get("expect", "answer") == "abstain":
        if gold:
            errors.append(f"{prefix}对抗样本（abstain）不应带 gold_facts")
        if banned:
            errors.append(f"{prefix}对抗样本不应带 must_not_include")

    return errors


def validate_records(records: Sequence[dict[str, Any]]) -> list[str]:
    """整批校验；返回全部错误（不早退，便于一次改完）。"""
    errors: list[str] = []
    for i, rec in enumerate(records):
        errors.extend(validate_record(rec, where=f"record[{i}]"))
    return errors


# ---------------------------------------------------------------- IO / CLI


def load_raw(path: str | Path) -> Any:
    """读取原始 JSON（顶层数组或包了一层的对象）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """读统一格式 JSONL，跳过空行。"""
    items: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(json.loads(line))
    return items


def write_jsonl(records: Iterable[dict[str, Any]], path: str | Path) -> int:
    """写统一格式 JSONL，返回条数。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = list(records)
    target.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    return len(rows)


DEFAULT_INPUT_NAMES: dict[str, tuple[str, ...]] = {
    "longmemeval": ("longmemeval_s.json", "longmemeval.json", "longmemeval_oracle.json"),
    "locomo": ("locomo10.json", "locomo.json"),
    "hotpotqa": ("hotpot_dev_distractor_v1.json", "hotpot_train_v1.1.json", "hotpotqa.json"),
    "freshqa": ("freshqa_v2.json", "freshqa.json", "questions.json"),
}


def _resolve_input(dataset: str, input_dir: Path) -> Path | None:
    """在 --input-dir 里按约定文件名找原始数据。"""
    for name in DEFAULT_INPUT_NAMES.get(dataset, ()):
        if (input_dir / name).exists():
            return input_dir / name
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval.datasets.public.convert", description="公开数据集 → 统一评测格式"
    )
    parser.add_argument("--dataset", default="all", choices=(*DATASETS, "all"))
    parser.add_argument("--input", default=None, help="原始 JSON 路径（单数据集）")
    parser.add_argument("--input-dir", default="raw", help="--dataset all 时按约定文件名查找")
    parser.add_argument("--output", default=None, help="输出 JSONL 路径（单数据集）")
    parser.add_argument("--output-dir", default="eval/datasets/public", help="--dataset all 时输出目录")
    parser.add_argument("--check-only", metavar="JSONL", default=None, help="只校验已有 JSONL，不转换")
    args = parser.parse_args(argv)

    if args.check_only:
        errors = validate_records(read_jsonl(args.check_only))
        for err in errors:
            print(f"[error] {err}", file=sys.stderr)
        print(f"校验 {args.check_only}：{'通过' if not errors else f'{len(errors)} 处错误'}")
        return 0 if not errors else 1

    targets = DATASETS if args.dataset == "all" else (args.dataset,)
    out_dir = Path(args.output_dir)
    failed = 0
    for dataset in targets:
        if args.input:
            raw_path = Path(args.input)
        else:
            raw_path = _resolve_input(dataset, Path(args.input_dir))
        if raw_path is None or not raw_path.exists():
            print(f"[skip] {dataset}: 未找到原始数据（--input 或 {args.input_dir}/）", file=sys.stderr)
            failed += 1
            continue

        records = CONVERTERS[dataset](load_raw(raw_path))
        errors = validate_records(records)
        if errors:
            for err in errors[:10]:
                print(f"[error] {dataset}: {err}", file=sys.stderr)
            failed += 1
            continue

        out_path = Path(args.output) if args.output else out_dir / f"{dataset}.gold.jsonl"
        n = write_jsonl(records, out_path)
        n_stale = sum(len(r["stale_facts"]) for r in records)
        n_abstain = sum(1 for r in records if r.get("expect") == "abstain")
        print(
            f"{dataset}: {n} 条记录 → {out_path}"
            f"（gold {sum(len(r['gold_facts']) for r in records)} 条，旧事实 {n_stale} 条，对抗 {n_abstain} 条）"
        )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
