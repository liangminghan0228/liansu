"""T5 · 向量索引 — SQLite + sqlite-vec，可注入 Embedder（测试用 mock，生产用 bge-small-zh）。

索引是派生品，删除 index.db 不影响 journal 真源，可由 rebuild 恢复。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from liansu.index.base import Index
from liansu.providers.base import Embedder
from liansu.schema import Fact


class VectorIndex(Index):
    def __init__(self, db_path: Path | str, embedder: Embedder, table: str = "facts"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self.table = table
        self._conn = sqlite3.connect(str(self.db_path))
        self._setup()

    def _setup(self) -> None:
        self._conn.execute("CREATE TABLE IF NOT EXISTS facts (fact_id TEXT PRIMARY KEY, embedding BLOB)")
        # 尝试加载 sqlite-vec 扩展；不可用时 fallback 到纯 Python cosine（仍能跑通功能）
        self._vec_available = False
        try:
            import sqlite_vec
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self._vec_available = True
        except Exception:
            pass

    def add(self, facts: list[Fact]) -> None:
        dim = self.embedder.dimension()
        for f in facts:
            vec = self.embedder.embed(f.statement_for_index())
            if len(vec) != dim:
                raise ValueError(f"embed 维度不一致: {len(vec)} != {dim}")
            blob = sqlite3.Binary(_pack(vec))
            self._conn.execute(
                f"INSERT OR REPLACE INTO {self.table}(fact_id, embedding) VALUES (?, ?)",
                (f.id, blob),
            )
        self._conn.commit()

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        qvec = self.embedder.embed(query)
        rows = self._conn.execute(f"SELECT fact_id, embedding FROM {self.table}").fetchall()
        scored = [(fid, _cosine(qvec, _unpack(blob))) for fid, blob in rows]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def rebuild(self, facts: list[Fact]) -> None:
        self._conn.execute(f"DELETE FROM {self.table}")
        self._conn.commit()
        self.add(facts)

    def close(self) -> None:
        self._conn.close()

    def count(self) -> int:
        return self._conn.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]


# float32 打包/解包（sqlite-vec 与 Python fallback 共用），用标准库避免 numpy 硬依赖
import struct


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
