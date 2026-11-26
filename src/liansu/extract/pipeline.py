"""T4 · 抽取管线编排（写路径）。

record(dialog) 全流程：
  gate -> extract -> confidence gate -> supersede 检查 -> append 真源 -> 增量更新索引
幂等：同一 (source, dialog_hash) 重复调用返回空，不产生重复事实。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from liansu import telemetry
from liansu.extract.extractor import DEFAULT_CONFIDENCE_THRESHOLD, extract_facts
from liansu.extract.gate import worth_recording
from liansu.index.base import Index
from liansu.providers.base import LLMProvider
from liansu.schema import Fact
from liansu.vault.journal import Journal


def _project_root() -> Path:
    from liansu.config import resolve_vault_root
    return resolve_vault_root()


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

    # ---- 遥测 ----
    def _telemetry_root(self) -> Path:
        """遥测落盘根：journal 目录的父目录即 vault 根（测试用 tmp_path 时自动隔离）。"""
        return Path(self.journal.root).parent

    def _telemetry_model(self) -> str | None:
        """provider 上的模型名（有的 provider 没有该属性）；取不到给 None。"""
        model = getattr(self.provider, "model", None)
        return model if isinstance(model, str) and model else None

    def _emit_extract(self, gate: str, written: list[Fact], superseded: list[str],
                      chars: int, ms: float) -> None:
        """写一条 extract 遥测；失败由 telemetry 吞掉，不影响写路径。"""
        telemetry.record_at(
            self._telemetry_root(), "extract",
            gate=gate,
            written=[fact.id for fact in written],
            superseded=list(superseded),
            chars=chars,
            ms=round(ms, 1),
            model=self._telemetry_model(),
        )

    # ---- 主流程 ----
    def record(self, dialog: list[dict[str, str]], source: str = "") -> list[Fact]:
        """gate → 抽取 → confidence → supersede → append 真源 → 增量索引。

        幂等命中（同 source + 同对话哈希）直接返回空且不写遥测；
        否则每次实际运行都写一条 ``kind="extract"`` 事件（gate 拒绝也算一次 runs）。
        """
        started = time.perf_counter()
        chars = sum(len(str(turn.get("content", ""))) for turn in dialog if isinstance(turn, dict))

        h = self.dialog_hash(dialog, source)
        if h in self._processed:
            return []

        if not worth_recording(dialog, self.provider):
            self._processed.add(h)
            self._save_processed()
            self._emit_extract("reject", [], [], chars, time.perf_counter() - started)
            return []

        facts = extract_facts(
            self.provider, dialog, source=source, confidence_threshold=self.confidence_threshold
        )
        written: list[Fact] = []
        superseded: list[str] = []
        for fact in facts:
            written.append(self._write_one(fact, superseded=superseded))

        if written and self.indexes:
            for idx in self.indexes:
                idx.add(written)

        self._processed.add(h)
        self._save_processed()
        self._emit_extract("pass", written, superseded, chars, time.perf_counter() - started)
        return written

    def _write_one(self, fact: Fact, superseded: list[str] | None = None) -> Fact:
        """单条落盘：命中同 subject+predicate 则 supersede 旧事实，否则纯 append。

        ``superseded`` 是可选输出参数：真正取代旧事实时把旧 id 追加进去，
        供遥测记录被取代的列表（完全同值的跳过不计）。
        """
        existing = self.journal.find_by_key(fact.subject, fact.predicate)
        if existing is not None and existing.id != fact.id:
            if existing.object == fact.object and existing.valid_from == fact.valid_from:
                return existing  # 完全同值，不必重复写
            fact = fact.model_copy(update={"supersedes": existing.id})
            self.journal.append(fact)
            self.journal.supersede(existing.id, fact.id, at=datetime.now(), when=fact.valid_from)
            if superseded is not None:
                superseded.append(existing.id)
        else:
            self.journal.append(fact)
        return fact
