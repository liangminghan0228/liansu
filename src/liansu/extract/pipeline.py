"""T4 · 抽取管线编排（写路径）。

record(dialog) 全流程：
  gate -> extract -> confidence gate -> supersede 检查 -> append 真源 -> 增量更新索引
幂等：同一 (source, dialog_hash) 重复调用返回空，不产生重复事实。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from liansu.extract.extractor import DEFAULT_CONFIDENCE_THRESHOLD, extract_facts
from liansu.extract.gate import worth_recording
from liansu.index.base import Index
from liansu.providers.base import LLMProvider
from liansu.schema import Fact
from liansu.vault.journal import Journal


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


class Pipeline:
    def __init__(
        self,
        journal: Journal,
        provider: LLMProvider,
        indexes: list[Index] | None = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        state_path: Path | str | None = None,
    ):
        self.journal = journal
        self.provider = provider
        self.indexes = indexes or []
        self.confidence_threshold = confidence_threshold
        self.state_path = Path(state_path) if state_path else _project_root() / ".liansu" / "processed.json"
        self._processed: set[str] = self._load_processed()

    # ---- 幂等状态 ----
    def _load_processed(self) -> set[str]:
        if self.state_path.exists():
            try:
                return set(json.loads(self.state_path.read_text(encoding="utf-8")))
            except Exception:
                return set()
        return set()

    def _save_processed(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(sorted(self._processed), ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def dialog_hash(dialog: list[dict[str, str]], source: str) -> str:
        payload = json.dumps({"source": source, "dialog": dialog}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ---- 主流程 ----
    def record(self, dialog: list[dict[str, str]], source: str = "") -> list[Fact]:
        h = self.dialog_hash(dialog, source)
        if h in self._processed:
            return []

        if not worth_recording(dialog, self.provider):
            self._processed.add(h)
            self._save_processed()
            return []

        facts = extract_facts(
            self.provider, dialog, source=source, confidence_threshold=self.confidence_threshold
        )
        written: list[Fact] = []
        for fact in facts:
            written.append(self._write_one(fact))

        if written and self.indexes:
            for idx in self.indexes:
                idx.add(written)

        self._processed.add(h)
        self._save_processed()
        return written

    def _write_one(self, fact: Fact) -> Fact:
        """单条落盘：命中同 subject+predicate 则 supersede 旧事实，否则纯 append。"""
        existing = self.journal.find_by_key(fact.subject, fact.predicate)
        if existing is not None and existing.id != fact.id:
            if existing.object == fact.object and existing.valid_from == fact.valid_from:
                return existing  # 完全同值，不必重复写
            fact = fact.model_copy(update={"supersedes": existing.id})
            self.journal.append(fact)
            self.journal.supersede(existing.id, fact.id, at=datetime.now(), when=fact.valid_from)
        else:
            self.journal.append(fact)
        return fact
