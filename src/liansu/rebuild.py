"""T12 · rebuild 全量重建 —— 删掉一切派生品后，一条命令从 journal 完全恢复。

顺序（TASKS.md T12）：
  ① 真源校验：replay 能完整重放、fact id 唯一、supersedes 依赖无环；
  ② 索引重建：对每个索引调 rebuild(全量 fact)。**索引收全量 fact**（含已被
     supersede、带 valid_to 的），T8 的 Stage2 消融要拿过期事实做对比；
  ③ 视图重建：views.rebuild()，视图内部只用有效 fact（真源 → 投影单向）；
  ④ 一致性报告：数量、检索抽样比对、issues。

脏真源（replay 抛错）直接中止——绝不允许把坏真源写成一堆空索引。
重复 id / supersede 指向缺失 fact / 依赖成环属于报告级问题：照常重建，
但 verified=False 并在 issues 里列明。

CLI：`python -m liansu.rebuild --verify`
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from datetime import date
from pathlib import Path

from liansu.config import load_config
from liansu.index.base import Index
from liansu.providers.base import Embedder
from liansu.schema import Fact
from liansu.vault.journal import Journal
from liansu.vault.views import ViewBuilder

log = logging.getLogger(__name__)

VERIFY_SAMPLES = 5
PROBE_TOP_K = 5


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- ① 真源校验


def _replay_source(journal: Journal) -> list[Fact]:
    """replay 必须能完整重放；失败即中止（空索引比不重建更危险）。"""
    try:
        return journal.replay()
    except Exception as exc:  # noqa: BLE001 —— 真源损坏需要带上下文抛出
        raise RuntimeError(f"journal replay 失败，中止重建: {exc}") from exc


def _find_supersede_cycle(facts: list[Fact]) -> list[str]:
    """沿 supersedes 链找环；返回环上的 id 序列（无环返回空）。"""
    links = {f.id: f.supersedes for f in facts if f.supersedes}
    for start in links:
        seen: dict[str, int] = {}
        path: list[str] = []
        cur: str | None = start
        while cur is not None and cur in links:
            if cur in seen:
                return path[seen[cur]:] + [cur]
            seen[cur] = len(path)
            path.append(cur)
            cur = links[cur]
    return []


def _integrity_issues(journal: Journal, facts: list[Fact]) -> list[str]:
    """真源结构性问题（报告级，不阻断重建）。"""
    issues: list[str] = []
    events = journal.all_events()
    written = [e["fact"]["id"] for e in events if e.get("event") == "fact"]

    dups = sorted(fid for fid, n in Counter(written).items() if n > 1)
    if dups:
        issues.append(f"重复 fact id（同 id 写入多次）: {dups[:5]}")

    known = {f.id for f in facts} | set(written)
    missing: set[str] = set()
    for ev in events:
        if ev.get("event") == "supersede" and ev.get("old_id") not in known:
            missing.add(str(ev.get("old_id")))
        if ev.get("event") == "supersede" and ev.get("new_id") not in known:
            missing.add(str(ev.get("new_id")))
    if missing:
        issues.append(f"supersede 指向不存在的 fact: {sorted(missing)[:5]}")

    cycle = _find_supersede_cycle(facts)
    if cycle:
        issues.append("supersedes 依赖成环: " + " -> ".join(cycle))
    return issues


# ---------------------------------------------------------------- ② 索引重建


def _index_size(idx: Index, facts: list[Fact]) -> int:
    """索引内 fact 条数：优先用索引自带 count/_facts，退化到逐条探针。"""
    for attr in ("count", "size"):
        fn = getattr(idx, attr, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:  # noqa: BLE001 —— 计数失败不影响重建
                pass
    store = getattr(idx, "_facts", None)
    if isinstance(store, dict):
        return len(store)

    seen: set[str] = set()
    top_k = max(PROBE_TOP_K, len(facts))
    for f in facts:
        try:
            seen.update(fid for fid, _ in idx.search(f.statement_for_index(), top_k=top_k))
        except Exception:  # noqa: BLE001
            break
    return len(seen)


# ---------------------------------------------------------------- ④ 抽样校验


def _sample(facts: list[Fact], n: int = VERIFY_SAMPLES) -> list[Fact]:
    """按 id 排序后均匀抽样，保证样本稳定可比。"""
    ordered = sorted(facts, key=lambda f: f.id)
    if len(ordered) <= n:
        return ordered
    step = len(ordered) / n
    return [ordered[int(i * step)] for i in range(n)]


def _probe(idx: Index, fact: Fact, top_k: int) -> list[str] | None:
    try:
        return [fid for fid, _ in idx.search(fact.statement_for_index(), top_k=top_k)]
    except Exception as exc:  # noqa: BLE001
        log.warning("index probe failed: %s", exc)
        return None


def _snapshot(indexes: dict[str, Index], samples: list[Fact]) -> dict[str, dict[str, list[str]]]:
    """重建前把每个索引对抽样 fact 的 TopK id 序列拍快照。"""
    snap: dict[str, dict[str, list[str]]] = {}
    for name, idx in indexes.items():
        per: dict[str, list[str]] = {}
        for f in samples:
            ids = _probe(idx, f, PROBE_TOP_K)
            if ids is not None:
                per[f.id] = ids
        snap[name] = per
    return snap


def _verify_snapshot(
    indexes: dict[str, Index],
    samples: list[Fact],
    before: dict[str, dict[str, list[str]]],
    facts_total: int,
) -> list[str]:
    """重建后对比：有旧结果则比 TopK id 集合，空索引则要求自检索命中。"""
    issues: list[str] = []
    full_k = max(PROBE_TOP_K, facts_total)
    for name, idx in indexes.items():
        for f in samples:
            after = _probe(idx, f, PROBE_TOP_K)
            if after is None:
                issues.append(f"index {name}: 抽样检索失败 ({f.id})")
                continue
            old = before.get(name, {}).get(f.id) or []
            if old and set(old) != set(after):
                issues.append(f"index {name}: 重建前后检索不一致 ({f.id})")
            elif not old:
                hit = _probe(idx, f, full_k) or []
                if f.id not in hit:
                    issues.append(f"index {name}: 重建后检索不到 {f.id}")
    return issues


# ---------------------------------------------------------------- 主流程


def rebuild(
    journal: Journal,
    indexes: dict[str, Index] | None = None,
    views: ViewBuilder | None = None,
    verify: bool = False,
    *,
    when: date | None = None,
) -> dict:
    """从 journal 全量重建索引与视图，返回一致性报告。

    verified 为 True 表示：无 issues 且（verify=True 时）抽样检索前后一致。
    """
    when = when or date.today()
    indexes = dict(indexes or {})

    facts = _replay_source(journal)
    issues = _integrity_issues(journal, facts)
    valid = [f for f in facts if f.is_valid_at(when)]

    samples = _sample(facts)
    before = _snapshot(indexes, samples) if verify else {}

    counts: dict[str, int] = {}
    for name, idx in indexes.items():
        try:
            idx.rebuild(facts)
        except Exception as exc:  # noqa: BLE001 —— 单个索引失败不拖垮其余重建
            counts[name] = 0
            issues.append(f"index {name}: 重建失败: {exc}")
            log.warning("index %s rebuild failed: %s", name, exc)
            continue
        counts[name] = _index_size(idx, facts)
        if facts and counts[name] == 0:
            issues.append(f"index {name}: 重建后为空")

    views_report: dict = {}
    if views is not None:
        try:
            views_report = views.rebuild() or {}
        except Exception as exc:  # noqa: BLE001
            issues.append(f"views: 重建失败: {exc}")
            log.warning("views rebuild failed: %s", exc)

    # 重建后再重放一次：真源没被重建过程改动
    try:
        after_facts = journal.replay()
    except Exception as exc:  # noqa: BLE001
        issues.append(f"重建后 replay 失败: {exc}")
    else:
        if len(after_facts) != len(facts):
            issues.append(
                f"重建前后 fact 数量不一致: {len(facts)} -> {len(after_facts)}"
            )
        after_valid = sum(1 for f in after_facts if f.is_valid_at(when))
        if after_valid != len(valid):
            issues.append(f"重建前后有效 fact 数量不一致: {len(valid)} -> {after_valid}")
        if views_report.get("facts") is not None and views is not None \
                and getattr(views, "journal", None) is journal \
                and views_report["facts"] != len(valid):
            issues.append(
                f"视图 fact 数与有效 fact 不一致: {views_report['facts']} != {len(valid)}"
            )

    if verify:
        issues.extend(_verify_snapshot(indexes, samples, before, len(facts)))

    return {
        "facts_total": len(facts),
        "facts_valid": len(valid),
        "indexes": counts,
        "views": views_report,
        "verified": not issues,
        "issues": issues,
    }


# ---------------------------------------------------------------- 默认装配


class BgeEmbedder(Embedder):
    """延迟加载的 bge-small-zh embedder：首次 embed 才 load 模型，CLI 启动不阻塞。"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5", device: str | None = None):
        self.model_name = model_name
        self.device = device
        self._model = None
        self._dim = 0

    def _ensure(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device=self.device)
            self._dim = int(self._model.get_sentence_embedding_dimension())
        return self._model

    def embed(self, text: str) -> list[float]:
        return [float(x) for x in self._ensure().encode(text, normalize_embeddings=True)]

    def dimension(self) -> int:
        self._ensure()
        return self._dim


def build_default_indexes(
    config: dict | None = None,
    base_dir: Path | str | None = None,
    include_vector: bool = True,
) -> tuple[dict[str, Index], list[str]]:
    """按 config 装配默认索引；vector 缺 embedder 时跳过，不整体失败。

    返回 (indexes, skipped)，skipped 为可直接写进报告的说明文案。
    """
    from liansu.index.bm25 import BM25Index
    from liansu.index.graph import GraphIndex

    cfg = config if config is not None else load_config()
    base = Path(base_dir) if base_dir is not None else _project_root()

    indexes: dict[str, Index] = {"bm25": BM25Index(), "graph": GraphIndex()}
    skipped: list[str] = []

    if not include_vector:
        skipped.append("vector: skipped (disabled)")
        return indexes, skipped

    try:
        import sentence_transformers  # noqa: F401  —— 只探测依赖是否可导入
    except Exception:  # noqa: BLE001
        skipped.append("vector: skipped (no embedder)")
        return indexes, skipped

    embedder_cfg = cfg.get("embedder") or {}
    db_path = base / (cfg.get("index") or {}).get("vector_db", ".liansu/index.db")
    try:
        from liansu.index.vector import VectorIndex

        indexes["vector"] = VectorIndex(
            db_path, BgeEmbedder(embedder_cfg.get("model", "BAAI/bge-small-zh-v1.5"))
        )
    except Exception as exc:  # noqa: BLE001 —— 向量库初始化失败降级为跳过
        skipped.append(f"vector: skipped (init failed: {exc})")
    return indexes, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m liansu.rebuild", description="连溯 — 从 journal 全量重建索引与视图"
    )
    parser.add_argument("--verify", action="store_true", help="重建后抽样比对检索结果")
    parser.add_argument("--root", help="vault 根目录（默认项目根）")
    parser.add_argument("--no-vector", action="store_true", help="跳过向量索引")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="[liansu] %(levelname)s %(message)s")

    cfg = load_config()
    root = Path(args.root).expanduser().resolve() if args.root else _project_root()
    journal = Journal(root / (cfg.get("vault") or {}).get("journal_dir", "journal"))

    indexes, skipped = build_default_indexes(
        config=cfg, base_dir=root, include_vector=not args.no_vector
    )
    try:
        report = rebuild(journal, indexes, ViewBuilder(root, journal), verify=args.verify)
    finally:
        for idx in indexes.values():
            try:
                idx.close()
            except Exception:  # noqa: BLE001
                pass

    if skipped:
        report["skipped"] = skipped
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
