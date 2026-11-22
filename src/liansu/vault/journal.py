"""T1 · journal 真源读写（写路径核心）。

设计要点（TASKS.md T1）：
- journal/YYYY-MM.md 分片 append-only，fcntl.flock 保证并发安全；
- 事件流追加两类事件 fact / supersede，不原地改旧行 —— 真源纯 append，
  时效状态在重放时计算（修复 TECH_DESIGN §3 "原地打 valid_to" 破坏 append-only 的问题）；
- iter_facts / replay 从事件流还原当前有效事实。
"""
from __future__ import annotations

import fcntl
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import yaml

from liansu.config import load_config
from liansu.schema import Fact


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


class Journal:
    """append-only 事实事件流。journal_root 可注入以便测试隔离。"""

    def __init__(self, root: Path | str | None = None):
        if root is None:
            cfg = load_config()
            root = _project_root() / cfg.get("vault", {}).get("journal_dir", "journal")
        self.root = Path(root)

    def _shard(self, when: date | None = None) -> Path:
        when = when or date.today()
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"{when:%Y-%m}.md"

    @staticmethod
    def _dump_envelope(envelope: dict) -> str:
        return "---\n" + yaml.safe_dump(envelope, allow_unicode=True, sort_keys=False)

    def _append_event(self, envelope: dict, when: date | None = None) -> None:
        shard = self._shard(when)
        block = self._dump_envelope(envelope)
        with shard.open("a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(block)
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def append(self, fact: Fact) -> Fact:
        """追加一条 fact 事件。"""
        self._append_event({"event": "fact", "fact": fact.to_dict()}, fact.valid_from)
        return fact

    def supersede(self, old_id: str, new_id: str, at: datetime | None = None, when: date | None = None) -> None:
        """追加失效事件：old 事实在 at 时刻被 new 覆盖（不修改旧行）。"""
        at = at or datetime.now()
        self._append_event(
            {"event": "supersede", "old_id": old_id, "new_id": new_id, "at": at.isoformat()},
            when,
        )

    def _iter_events(self) -> Iterator[dict]:
        if not self.root.exists():
            return
        for shard in sorted(self.root.glob("*.md")):
            text = shard.read_text(encoding="utf-8")
            for doc in yaml.safe_load_all(text):
                if isinstance(doc, dict) and "event" in doc:
                    yield doc

    def replay(self) -> list[Fact]:
        """从空状态重放全部事件，返回当前有效 fact（含被 supersede 者带 valid_to）。"""
        facts: dict[str, Fact] = {}
        superseded_at: dict[str, date] = {}
        for ev in self._iter_events():
            if ev["event"] == "fact":
                facts[ev["fact"]["id"]] = Fact.from_dict(ev["fact"])
            elif ev["event"] == "supersede":
                at = datetime.fromisoformat(ev["at"])
                superseded_at[ev["old_id"]] = at.date()
        for fid, when in superseded_at.items():
            if fid in facts:
                facts[fid] = facts[fid].model_copy(update={"valid_to": when})
        return list(facts.values())

    def iter_facts(self, valid_only: bool = True, when: date | None = None) -> Iterator[Fact]:
        when = when or date.today()
        for f in self.replay():
            if not valid_only or f.is_valid_at(when):
                yield f

    def get_by_id(self, fact_id: str) -> Fact | None:
        for f in self.replay():
            if f.id == fact_id:
                return f
        return None

    def find_by_key(self, subject: str, predicate: str, valid_only: bool = True) -> Fact | None:
        """按 subject+predicate 查重；命中当前有效 fact 则返回（供 supersede 去重）。"""
        for f in self.iter_facts(valid_only=valid_only):
            if f.subject == subject and f.predicate == predicate:
                return f
        return None

    def all_events(self) -> list[dict]:
        return list(self._iter_events())
