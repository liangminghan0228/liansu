"""T4 抽取管线：gate / confidence / supersede / 幂等 / 索引增量。"""
import json
from datetime import date
from pathlib import Path

import pytest

from liansu.extract.pipeline import Pipeline
from liansu.schema import Fact
from liansu.vault.journal import Journal

from tests.fixtures.mock_index import MockIndex
from tests.fixtures.mock_provider import MockProvider


def make_fact(fid, obj="北京", conf=0.9, **kw):
    base = dict(id=fid, subject="梁明晗", predicate="求职目标城市", object=obj,
                statement=f"梁明晗求职目标城市{obj}", valid_from=date(2026, 11, 21),
                confidence=conf, topics=["求职"], source="test://")
    base.update(kw)
    return Fact.from_dict(base)


DIALOG = [
    {"role": "user", "content": "我决定求职目标城市就定北京了，西安作为备选。"},
    {"role": "assistant", "content": "好的，已记下。"},
]


@pytest.fixture
def pipeline(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    idx = MockIndex()
    provider = MockProvider(facts=[make_fact("F-1")])
    p = Pipeline(journal, provider, indexes=[idx], state_path=tmp_path / "processed.json")
    return p, journal, idx, provider


def test_record_writes_journal_and_index(pipeline):
    p, journal, idx, _ = pipeline
    facts = p.record(DIALOG, source="test://d1")
    assert len(facts) == 1
    assert journal.get_by_id("F-1") is not None
    assert idx.search("北京")  # 索引已增量写入


def test_confidence_filter(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    provider = MockProvider(facts=[make_fact("F-low", conf=0.5), make_fact("F-ok", conf=0.8)])
    p = Pipeline(journal, provider, state_path=tmp_path / "p.json")
    facts = p.record(DIALOG, source="s")
    assert [f.id for f in facts] == ["F-ok"]


def test_gate_false_skips(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    provider = MockProvider(gate=False, facts=[make_fact("F-x")])
    p = Pipeline(journal, provider, state_path=tmp_path / "p.json")
    assert p.record(DIALOG, source="s") == []
    assert list(journal.iter_facts()) == []


def test_supersede_on_same_key(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    # 第一次写入旧值，第二次同 subject+predicate 新值 -> supersede
    provider = MockProvider(facts=[make_fact("F-old", obj="北京/西安未定")])
    p = Pipeline(journal, provider, state_path=tmp_path / "p.json")
    p.record(DIALOG, source="s1")

    provider2 = MockProvider(facts=[make_fact("F-new", obj="北京")])
    p2 = Pipeline(journal, provider2, state_path=tmp_path / "p.json")
    p2.record(DIALOG, source="s2")

    old = journal.get_by_id("F-old")
    assert old.valid_to is not None            # 旧事实被打上失效日
    assert journal.get_by_id("F-new").supersedes == "F-old"
    valid = [f.id for f in journal.iter_facts()]
    assert valid == ["F-new"]


def test_idempotent(pipeline):
    p, journal, _, provider = pipeline
    p.record(DIALOG, source="same")
    n_events = len(journal.all_events())
    p.record(DIALOG, source="same")            # 同 source + 同 dialog -> 幂等
    assert len(journal.all_events()) == n_events
    assert provider.calls.count("extract") == 1


def test_rule_gate_blocks_chitchat(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    provider = MockProvider(facts=[make_fact("F-1")])
    p = Pipeline(journal, provider, state_path=tmp_path / "p.json")
    assert p.record([{"role": "user", "content": "好的谢谢"}], source="s") == []
    assert provider.calls == []                # 规则层否决，未调 LLM
