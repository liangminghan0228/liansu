"""T11 派生视图：entities / topics / brief 生成、全量重建、只读约束。"""
from datetime import date, datetime
from pathlib import Path

from liansu.schema import Fact
from liansu.vault.journal import Journal
from liansu.vault.views import ViewBuilder


def make_fact(fid: str, subject: str = "梁明晗", predicate: str = "求职目标城市",
              obj: str = "北京", **kw) -> Fact:
    base = dict(
        id=fid, subject=subject, predicate=predicate, object=obj,
        statement=f"{subject}的{predicate}是{obj}",
        valid_from=date(2026, 11, 21), confidence=0.9,
        topics=["求职"], source=f"codex://{fid}",
    )
    base.update(kw)
    return Fact.from_dict(base)


def seed(journal: Journal) -> None:
    """有效 fact + 低置信 + 已过期 + 另一 subject，供各视图分支使用。"""
    journal.append(make_fact("F-1", statement="梁明晗决定求职目标城市定为北京",
                             confidence=0.92, topics=["求职", "城市"]))
    journal.append(make_fact("F-2", predicate="身高", obj="188cm",
                             statement="梁明晗身高188cm", confidence=0.85, topics=["健康"]))
    journal.append(make_fact("F-3", predicate="宠物", obj="猫",
                             statement="梁明晗养了一只猫", confidence=0.4, topics=["生活"]))
    journal.append(make_fact("F-4", subject="腾讯", predicate="办公地", obj="深圳",
                             statement="腾讯办公地在深圳", confidence=0.9, topics=["城市", "公司"]))
    journal.append(make_fact("F-5", subject="历史公司", statement="历史公司的旧事实",
                             confidence=0.95, topics=["城市"],
                             valid_from=date(2020, 1, 1), valid_to=date(2020, 2, 1)))


def test_entities_content(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    seed(journal)
    stats = ViewBuilder(tmp_path / "vault", journal).rebuild()

    assert stats == {"entities": 2, "topics": 5, "facts": 4}
    ent = (tmp_path / "vault" / "entities" / "梁明晗.md").read_text(encoding="utf-8")
    assert ent.startswith("# 梁明晗")
    assert "梁明晗决定求职目标城市定为北京" in ent
    assert "2026-11-21" in ent          # valid_from
    assert "codex://F-1" in ent         # source
    assert "[[梁明晗]]" in ent and "[[腾讯]]" in ent  # wikilinks 互链
    # 已过期的 F-5 不应出现在任何实体视图
    assert not any("历史公司的旧事实" in p.read_text(encoding="utf-8")
                   for p in (tmp_path / "vault" / "entities").glob("*.md"))


def test_topics_one_file_per_tag(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    seed(journal)
    ViewBuilder(tmp_path / "vault", journal).rebuild()
    topics_dir = tmp_path / "vault" / "topics"
    names = {p.stem for p in topics_dir.glob("*.md")}
    assert names == {"求职", "城市", "健康", "生活", "公司"}
    city = (topics_dir / "城市.md").read_text(encoding="utf-8")
    assert "F-1" in city and "F-4" in city       # 两条带「城市」标签的 fact
    assert "梁明晗决定求职目标城市定为北京" in city


def test_brief_groups_and_filters(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    seed(journal)
    ViewBuilder(tmp_path / "vault", journal).rebuild()
    brief = (tmp_path / "vault" / "brief.md").read_text(encoding="utf-8")

    assert "## 梁明晗" in brief and "## 腾讯" in brief  # 按 subject 分组
    assert "梁明晗决定求职目标城市定为北京" in brief
    assert "腾讯办公地在深圳" in brief
    assert "养了一只猫" not in brief           # 低置信被过滤
    assert "历史公司的旧事实" not in brief     # 已过期被过滤
    assert len(brief) <= 2000                   # token 粗控


def test_rebuild_idempotent_and_no_writeback(tmp_path: Path):
    journal_root = tmp_path / "journal"
    journal = Journal(journal_root)
    seed(journal)
    builder = ViewBuilder(tmp_path / "vault", journal)

    before = {p.name: p.read_bytes() for p in journal_root.glob("*.md")}
    stats = builder.rebuild()
    after_first = _snapshot(tmp_path / "vault")

    # 删掉整个 views 目录后重建，内容应完全一致
    for d in ("entities", "topics"):
        _rmtree(tmp_path / "vault" / d)
    (tmp_path / "vault" / "brief.md").unlink()
    stats2 = builder.rebuild()
    after_second = _snapshot(tmp_path / "vault")

    assert stats == stats2
    assert after_first == after_second
    # 只读约束：journal 文件（名字与字节）未被改动
    after_journal = {p.name: p.read_bytes() for p in journal_root.glob("*.md")}
    assert before == after_journal


def test_superseded_fact_dropped(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    old = make_fact("F-old", obj="北京/西安未定", statement="尚未决定城市")
    new = make_fact("F-new", obj="北京", statement="已定为北京", supersedes="F-old")
    journal.append(old)
    journal.append(new)
    journal.supersede("F-old", "F-new", at=datetime(2026, 11, 22, 10, 0))

    stats = ViewBuilder(tmp_path / "vault", journal).rebuild()
    assert stats["facts"] == 1
    ent = (tmp_path / "vault" / "entities" / "梁明晗.md").read_text(encoding="utf-8")
    assert "已定为北京" in ent and "尚未决定城市" not in ent


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8")
            for p in root.rglob("*.md")}


def _rmtree(path: Path) -> None:
    import shutil
    if path.exists():
        shutil.rmtree(path)
