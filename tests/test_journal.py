"""T1 journal：并发 append / supersede 时效 / 分片 / 幂等审计。"""
import concurrent.futures
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from liansu.schema import Fact
from liansu.vault.journal import Journal


def make_fact(fid: str, subject: str = "梁明晗", predicate: str = "求职目标城市", obj: str = "北京", **kw):
    base = dict(
        id=fid, subject=subject, predicate=predicate, object=obj,
        statement=f"{subject}的{predicate}是{obj}", valid_from=date(2026, 11, 21),
        confidence=0.9, topics=["求职"], source="test://",
    )
    base.update(kw)
    return Fact.from_dict(base)


def test_append_and_replay(tmp_path: Path):
    j = Journal(tmp_path)
    f = make_fact("F-001")
    j.append(f)
    assert j.get_by_id("F-001") is not None
    assert len(list(j.iter_facts())) == 1
    assert (tmp_path / "2026-11.md").exists()


def test_supersede_sets_valid_to(tmp_path: Path):
    j = Journal(tmp_path)
    old = make_fact("F-old", obj="北京/西安未定")
    new = make_fact("F-new", obj="北京", supersedes="F-old")
    j.append(old)
    j.append(new)
    j.supersede("F-old", "F-new", at=datetime(2026, 11, 22, 10, 0))

    assert j.get_by_id("F-old").valid_to == date(2026, 11, 22)
    # 有效视图只剩新 fact；旧行仍在 replay（append-only 未被改写）
    valid = list(j.iter_facts())
    assert [f.id for f in valid] == ["F-new"]
    assert len(j.replay()) == 2


def test_valid_only_future(tmp_path: Path):
    j = Journal(tmp_path)
    j.append(make_fact("F-1"))
    j.supersede("F-1", "F-2", at=datetime(2026, 11, 22))
    # replay 得到的是「当前状态」：F-1 已被打上 valid_to，失效日当天起不再有效
    assert j.get_by_id("F-1").valid_to == date(2026, 11, 22)
    assert [f.id for f in j.iter_facts(when=date(2026, 11, 21))] == ["F-1"]
    assert list(j.iter_facts(when=date(2026, 11, 22))) == []


def test_find_by_key(tmp_path: Path):
    j = Journal(tmp_path)
    j.append(make_fact("F-a", predicate="身高", obj="188"))
    assert j.find_by_key("梁明晗", "身高").id == "F-a"
    assert j.find_by_key("梁明晗", "体重") is None


def test_concurrent_append_100(tmp_path: Path):
    j = Journal(tmp_path)

    def writer(i):
        j.append(make_fact(f"F-{i:03d}", predicate="序号", obj=str(i)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(writer, range(100)))
    facts = list(j.iter_facts())
    ids = [f.id for f in facts]
    assert len(ids) == 100 and len(set(ids)) == 100
