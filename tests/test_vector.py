"""T5 向量索引：用 mock embedder 验证 add/search/rebuild，不下载真实模型。"""
from datetime import date
from pathlib import Path

import pytest

from liansu.index.vector import VectorIndex
from liansu.providers.base import Embedder
from liansu.schema import Fact

try:
    from tests.fixtures.mock_provider import MockProvider  # noqa: F401
except ImportError:
    pass


class HashEmbedder(Embedder):
    """确定性 8 维 hash embedding，保证相同文本同向量、无关文本低相似。"""

    def __init__(self, dim: int = 8):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in text:
            vec[ord(tok) % self.dim] += 1.0
        # 加入 bigram 提升区分度
        for i in range(len(text) - 1):
            vec[(ord(text[i]) + ord(text[i + 1])) % self.dim] += 0.5
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]

    def dimension(self) -> int:
        return self.dim


def make_fact(fid: str, text: str, **kw):
    base = dict(id=fid, subject="梁明晗", predicate="测试", object=text,
                statement=text, valid_from=date(2026, 11, 21), confidence=0.9,
                topics=["测试"], source="test://")
    base.update(kw)
    return Fact.from_dict(base)


@pytest.fixture
def facts():
    return [
        make_fact("F-1", "梁明晗求职目标城市是北京"),
        make_fact("F-2", "梁明晗身高188体重82"),
        make_fact("F-3", "梁明晗会RAG和多Agent编排"),
        make_fact("F-4", "今天天气很好适合出门散步"),
    ]


def test_add_search(tmp_path: Path, facts):
    idx = VectorIndex(tmp_path / "index.db", HashEmbedder())
    idx.add(facts)
    assert idx.count() == 4
    res = idx.search("求职目标城市", top_k=2)
    assert len(res) == 2
    assert all(fid.startswith("F-") for fid, _ in res)
    idx.close()


def test_rebuild_consistency(tmp_path: Path, facts):
    idx = VectorIndex(tmp_path / "index.db", HashEmbedder())
    idx.add(facts)
    before = idx.search("RAG", top_k=3)
    idx.rebuild(facts)
    after = idx.search("RAG", top_k=3)
    assert before == after
    assert idx.count() == 4
    idx.close()


def test_index_is_derived(tmp_path: Path, facts):
    """删除 index.db 后重建，结果一致 —— 索引是派生品。"""
    db = tmp_path / "index.db"
    idx = VectorIndex(db, HashEmbedder())
    idx.add(facts)
    expected = idx.search("身高", top_k=2)
    idx.close()
    db.unlink()
    idx2 = VectorIndex(db, HashEmbedder())
    idx2.rebuild(facts)
    assert idx2.search("身高", top_k=2) == expected
    idx2.close()


def test_empty_search(tmp_path: Path):
    idx = VectorIndex(tmp_path / "index.db", HashEmbedder())
    assert idx.search("任意") == []
    idx.close()
