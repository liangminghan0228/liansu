"""T12 rebuild：真源校验 → 索引/视图全量重建 → 一致性报告。"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime
from pathlib import Path

import pytest

from liansu.index.bm25 import BM25Index
from liansu.index.graph import GraphIndex
from liansu.index.vector import VectorIndex
from liansu.providers.base import Embedder
from liansu.rebuild import main, rebuild
from liansu.schema import Fact
from liansu.vault.journal import Journal
from liansu.vault.views import ViewBuilder


class HashEmbedder(Embedder):
    """8 维确定性 hash embedder：同文本必得同向量，供 rebuild 前后逐位对比。"""

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = [b / 255.0 for b in digest[:8]]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def dimension(self) -> int:
        return 8


def _fact(
    fid: str,
    subject: str,
    predicate: str,
    obj: str,
    statement: str | None = None,
    topics: list[str] | None = None,
    valid_from: date = date(2024, 1, 1),
    **kw,
) -> Fact:
    return Fact(
        id=fid,
        subject=subject,
        predicate=predicate,
        object=obj,
        statement=statement or f"{subject}{predicate}是{obj}",
        valid_from=valid_from,
        confidence=0.9,
        topics=topics or ["求职"],
        source="test://",
        **kw,
    )


def _vault(tmp_path: Path) -> Journal:
    """5 条 fact：F-old 在 2024-06-01 被 F-new supersede，有效 4 条。"""
    j = Journal(tmp_path / "journal")
    j.append(_fact("F-1", "梁明晗", "求职目标城市", "北京",
                   statement="梁明晗的求职目标城市是北京", topics=["求职", "城市"]))
    j.append(_fact("F-2", "梁明晗", "毕业院校", "某某大学",
                   statement="梁明晗毕业于某某大学", topics=["教育"]))
    j.append(_fact("F-3", "某某科技", "所在城市", "上海",
                   statement="某某科技总部在上海", topics=["公司"]))
    j.append(_fact("F-old", "梁明晗", "偏好城市", "西安",
                   statement="梁明晗偏好城市是西安", topics=["求职", "城市"]))
    j.append(_fact("F-new", "梁明晗", "偏好城市", "北京",
                   statement="梁明晗改定偏好城市为北京", topics=["求职", "城市"],
                   valid_from=date(2024, 6, 1), supersedes="F-old"))
    j.supersede("F-old", "F-new", at=datetime(2024, 6, 1, 10, 0))
    return j


def _indexes(tmp_path: Path) -> dict:
    return {
        "bm25": BM25Index(),
        "graph": GraphIndex(),
        "vector": VectorIndex(tmp_path / "index.db", HashEmbedder()),
    }


def _ids(hits) -> list[str]:
    return [fid for fid, _ in hits]


def _close_all(indexes: dict) -> None:
    for idx in indexes.values():
        idx.close()


def test_rebuild_makes_indexes_searchable(tmp_path: Path):
    j = _vault(tmp_path)
    indexes = _indexes(tmp_path)
    try:
        rebuild(j, indexes, ViewBuilder(tmp_path, j))
        assert "F-1" in _ids(indexes["bm25"].search("北京", top_k=5))
        assert "F-2" in _ids(indexes["graph"].search("某某大学", top_k=5))
        assert "F-3" in _ids(indexes["vector"].search("某某科技总部在上海", top_k=5))
    finally:
        _close_all(indexes)


def test_report_counts(tmp_path: Path):
    j = _vault(tmp_path)
    indexes = _indexes(tmp_path)
    try:
        report = rebuild(j, indexes, ViewBuilder(tmp_path, j))
    finally:
        _close_all(indexes)

    assert report["facts_total"] == 5
    assert report["facts_valid"] == 4
    assert report["indexes"] == {"bm25": 5, "graph": 5, "vector": 5}
    assert report["views"] == {"entities": 2, "topics": 4, "facts": 4}
    assert report["verified"] is True
    assert report["issues"] == []


def test_rebuild_recovers_after_deleting_derived_artifacts(tmp_path: Path):
    j = _vault(tmp_path)
    indexes = _indexes(tmp_path)
    rebuild(j, indexes, ViewBuilder(tmp_path, j))
    _close_all(indexes)

    # 删掉一切派生品：索引库 + 视图目录 + brief
    assert (tmp_path / "index.db").exists()
    assert (tmp_path / "entities" / "梁明晗.md").exists()
    (tmp_path / "index.db").unlink()
    shutil.rmtree(tmp_path / "entities")
    shutil.rmtree(tmp_path / "topics")
    (tmp_path / "brief.md").unlink()

    fresh = _indexes(tmp_path)
    try:
        report = rebuild(j, fresh, ViewBuilder(tmp_path, j))
        assert report["verified"] is True, report["issues"]
        assert "F-1" in _ids(fresh["bm25"].search("北京", top_k=5))
        assert "F-3" in _ids(fresh["vector"].search("某某科技总部在上海", top_k=5))
        assert fresh["graph"].find_fact("F-old") is not None
        assert (tmp_path / "entities" / "梁明晗.md").exists()
        assert (tmp_path / "topics" / "求职.md").exists()
        assert (tmp_path / "brief.md").read_text(encoding="utf-8").startswith("# 记忆摘要")
    finally:
        _close_all(fresh)


def test_verify_reports_stable_search_results(tmp_path: Path):
    j = _vault(tmp_path)
    indexes = _indexes(tmp_path)
    views = ViewBuilder(tmp_path, j)
    try:
        first = rebuild(j, indexes, views)
        assert first["verified"] is True
        query = "偏好城市 北京"
        before = {name: _ids(idx.search(query, top_k=5)) for name, idx in indexes.items()}

        second = rebuild(j, indexes, views, verify=True)
        assert second["issues"] == []
        assert second["verified"] is True

        for name, idx in indexes.items():
            assert _ids(idx.search(query, top_k=5)) == before[name]
    finally:
        _close_all(indexes)


def test_rebuild_is_idempotent(tmp_path: Path):
    j = _vault(tmp_path)
    indexes = _indexes(tmp_path)
    views = ViewBuilder(tmp_path, j)
    try:
        first = rebuild(j, indexes, views, verify=True)
        first_snapshot = {n: _ids(idx.search("梁明晗", top_k=5)) for n, idx in indexes.items()}
        second = rebuild(j, indexes, views, verify=True)
        second_snapshot = {n: _ids(idx.search("梁明晗", top_k=5)) for n, idx in indexes.items()}
    finally:
        _close_all(indexes)

    assert first["indexes"] == second["indexes"]
    assert first["facts_total"] == second["facts_total"] == 5
    assert second["issues"] == []
    assert second_snapshot == first_snapshot


def test_rebuild_without_indexes_or_views(tmp_path: Path):
    j = _vault(tmp_path)
    report = rebuild(j, {}, None)
    assert report["indexes"] == {}
    assert report["views"] == {}
    assert report["facts_total"] == 5
    assert report["facts_valid"] == 4
    assert report["verified"] is True

    # 连 indexes=None 也允许
    assert rebuild(j, None)["facts_total"] == 5


def test_expired_facts_stay_in_indexes(tmp_path: Path):
    """Stage2 消融需要过期事实仍在索引内；视图则只收有效事实。"""
    j = _vault(tmp_path)
    indexes = _indexes(tmp_path)
    try:
        report = rebuild(j, indexes, ViewBuilder(tmp_path, j), verify=True)
        assert report["facts_total"] == 5
        assert report["facts_valid"] == 4

        assert "F-old" in _ids(indexes["bm25"].search("西安", top_k=5))
        assert indexes["graph"].find_fact("F-old").valid_to == date(2024, 6, 1)
        assert "F-old" in _ids(indexes["vector"].search("梁明晗偏好城市是西安", top_k=10))

        entity_md = (tmp_path / "entities" / "梁明晗.md").read_text(encoding="utf-8")
        assert "西安" not in entity_md
        assert "改定偏好城市为北京" in entity_md
    finally:
        _close_all(indexes)


def test_integrity_issues_are_reported(tmp_path: Path):
    j = Journal(tmp_path / "journal")
    j.append(_fact("F-a", "甲", "依赖", "乙", supersedes="F-b"))
    j.append(_fact("F-b", "乙", "依赖", "甲", supersedes="F-a"))
    j.append(_fact("F-dup", "丙", "重复", "一"))
    j.append(_fact("F-dup", "丙", "重复", "二"))

    report = rebuild(j, {})

    assert report["facts_total"] == 3  # 重复 id 重放后只算一条
    assert report["verified"] is False
    assert any("重复 fact id" in issue for issue in report["issues"])
    assert any("成环" in issue for issue in report["issues"])


def test_replay_failure_aborts_rebuild(tmp_path: Path):
    j = Journal(tmp_path / "journal")
    j.append(_fact("F-1", "甲", "谓词", "乙"))
    (tmp_path / "journal" / "2024-01.md").write_text("---\nfact: [未闭合\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="replay 失败"):
        rebuild(j, {"bm25": BM25Index()})


def test_cli_smoke(tmp_path: Path, capsys):
    _vault(tmp_path)
    code = main(["--root", str(tmp_path), "--no-vector", "--verify"])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["facts_total"] == 5
    assert out["facts_valid"] == 4
    assert "bm25" in out["indexes"] and "graph" in out["indexes"]
    assert out["skipped"] == ["vector: skipped (disabled)"]
    assert (tmp_path / "brief.md").exists()
