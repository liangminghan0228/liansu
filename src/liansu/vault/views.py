"""T11 · 派生视图生成（entities / topics / brief）。

视图是 journal 真源的只读投影：每次 rebuild() 全量重算，
先清空生成目录再写，绝不反向写回 journal。journal 可注入以便测试隔离。
"""
from __future__ import annotations

import re
import shutil
from collections import defaultdict
from pathlib import Path

from liansu.config import load_config
from liansu.schema import Fact
from liansu.vault.journal import Journal

# Windows/macOS/Linux 文件名非法字符
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_name(name: str) -> str:
    """把 subject / tag 安全化为文件名（非法字符替换为下划线）。"""
    name = _ILLEGAL.sub("_", name.strip()).strip(". ")
    return name or "unnamed"


class ViewBuilder:
    """从 Journal 生成 Markdown 派生视图。vault_root 与 journal 均可注入。"""

    BRIEF_MAX_CHARS = 2000
    BRIEF_CONFIDENCE = 0.8

    def __init__(self, vault_root: Path | str, journal: Journal | None = None):
        self.root = Path(vault_root)
        vault_cfg = load_config().get("vault", {})
        self.entities_dir = self.root / vault_cfg.get("entities_dir", "entities")
        self.topics_dir = self.root / vault_cfg.get("topics_dir", "topics")
        self.brief_file = self.root / vault_cfg.get("brief_file", "brief.md")
        self.journal = journal if journal is not None else Journal(
            self.root / vault_cfg.get("journal_dir", "journal")
        )

    @staticmethod
    def _reset_dir(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)

    def rebuild(self) -> dict:
        """全量重算所有视图，返回统计 dict。"""
        facts = list(self.journal.iter_facts(valid_only=True))
        entity_count = self._build_entities(facts)
        topic_count = self._build_topics(facts)
        self._build_brief(facts)
        return {"entities": entity_count, "topics": topic_count, "facts": len(facts)}

    def _build_entities(self, facts: list[Fact]) -> int:
        self._reset_dir(self.entities_dir)
        by_subject: dict[str, list[Fact]] = defaultdict(list)
        for f in facts:
            by_subject[f.subject].append(f)

        for subject, group in by_subject.items():
            group.sort(key=lambda f: (f.valid_from, f.id))
            lines = [f"# {subject}", ""]
            others = [s for s in by_subject if s != subject]
            if others:
                lines.append("相关实体：" + " · ".join(f"[[{s}]]" for s in others))
                lines.append("")
            for f in group:
                lines.append(f"## {f.statement}")
                lines.append(f"- 实体：[[{subject}]]")
                lines.append(f"- 生效：{f.valid_from.isoformat()}")
                lines.append(f"- 来源：{f.source}")
                lines.append("")
            (self.entities_dir / f"{_safe_name(subject)}.md").write_text(
                "\n".join(lines), encoding="utf-8"
            )
        return len(by_subject)

    def _build_topics(self, facts: list[Fact]) -> int:
        self._reset_dir(self.topics_dir)
        by_topic: dict[str, list[Fact]] = defaultdict(list)
        for f in facts:
            for tag in f.topics:
                by_topic[tag].append(f)

        for tag, group in by_topic.items():
            lines = [f"# #{tag}", ""]
            for f in group:
                lines.append(f"- [[{f.subject}]] · {f.statement}")
                lines.append(f"  - 生效：{f.valid_from.isoformat()} · 来源：{f.source}")
            (self.topics_dir / f"{_safe_name(tag)}.md").write_text(
                "\n".join(lines), encoding="utf-8"
            )
        return len(by_topic)

    def _build_brief(self, facts: list[Fact]) -> int:
        selected = [
            f for f in facts
            if f.confidence >= self.BRIEF_CONFIDENCE and f.is_valid_at()
        ]
        by_subject: dict[str, list[Fact]] = defaultdict(list)
        for f in selected:
            by_subject[f.subject].append(f)

        parts = ["# 记忆摘要", ""]
        total = len("\n".join(parts))
        for subject, group in by_subject.items():
            block = [f"## {subject}"] + [f"- {f.statement}" for f in group]
            text = "\n".join(block)
            needed = len(text) + 2  # 末尾换行分隔
            if total + needed > self.BRIEF_MAX_CHARS:
                break
            parts.extend([text, ""])
            total += needed

        content = "\n".join(parts).rstrip() + "\n"
        if len(content) > self.BRIEF_MAX_CHARS:
            content = content[: self.BRIEF_MAX_CHARS]
        self.brief_file.parent.mkdir(parents=True, exist_ok=True)
        self.brief_file.write_text(content, encoding="utf-8")
        return len(selected)
