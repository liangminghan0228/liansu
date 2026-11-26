"""T16 metrics：窗口/会话过滤、聚合口径、vault 体量、CLI JSON 契约。"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from liansu import cli, metrics, telemetry
from liansu.schema import Fact
from liansu.vault.journal import Journal


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("LIANSU_TELEMETRY", raising=False)
    monkeypatch.delenv("LIANSU_SESSION", raising=False)
    monkeypatch.delenv("LIANSU_ROOT", raising=False)


def _iso(days_ago: int = 0) -> str:
    return (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _append(root: Path, days_ago: int = 0, **payload) -> dict:
    """直接按契约追加一条事件（可指定 days_ago，用于窗口测试）。"""
    event = {"t": _iso(days_ago), "session": None, "pid": 123, "v": 1}
    event.update(payload)
    path = telemetry.events_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def _extract(root: Path, gate: str = "pass", written=None, superseded=None,
             ms: float | None = 100, days_ago: int = 0, **extra):
    return _append(root, days_ago, kind="extract", gate=gate, written=written or [],
                   superseded=superseded or [], chars=10, ms=ms, model="local", **extra)


def _make_fact(fid: str, obj: str = "北京", valid_from: date | None = None) -> Fact:
    return Fact.from_dict({
        "id": fid, "subject": "梁明晗", "predicate": "求职目标城市", "object": obj,
        "statement": f"梁明晗求职目标城市{obj}",
        "valid_from": (valid_from or date.today()).isoformat(),
        "confidence": 0.9, "topics": ["求职"], "source": "test://",
    })


# ---------------------------------------------------------------- 空数据契约


def test_empty_vault_contract(tmp_path: Path):
    payload = metrics.build_metrics(root=tmp_path)

    assert set(payload) == {"generatedAt", "windowDays", "global", "session", "vault", "recent"}
    assert payload["windowDays"] == 7
    assert payload["session"] is None
    assert payload["recent"] == []
    assert payload["vault"] == {"facts": 0, "stale": 0, "entities": 0, "topics": 0,
                                "journalBytes": 0}
    datetime.fromisoformat(payload["generatedAt"])

    g = payload["global"]
    assert set(g) == {"counts", "extract", "recall", "latency", "daily", "lastBrief"}
    assert g["counts"] == {"extract": 0, "recall": 0, "expand": 0, "brief": 0, "rebuild": 0}
    assert g["extract"] == {"runs": 0, "passed": 0, "rejected": 0, "facts": 0, "superseded": 0}
    assert g["recall"] == {"calls": 0, "results": 0, "staleFiltered": 0, "avgTopK": 0}
    assert g["latency"] == {
        "extract": {"p50": 0, "p95": 0, "n": 0},
        "recall": {"p50": 0, "p95": 0, "n": 0},
        "brief": {"p50": 0, "p95": 0, "n": 0},
    }
    assert len(g["daily"]) == 7
    assert g["daily"][-1] == {"date": date.today().isoformat(), "extracts": 0,
                              "facts": 0, "recalls": 0}
    assert [row["date"] for row in g["daily"]] == [
        (date.today() - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)
    ]
    assert g["lastBrief"] is None


def test_days_argument_controls_daily_span(tmp_path: Path):
    payload = metrics.build_metrics(root=tmp_path, days=3)
    assert payload["windowDays"] == 3
    assert len(payload["global"]["daily"]) == 3


# ---------------------------------------------------------------- 聚合口径


def test_extract_aggregates(tmp_path: Path):
    _extract(tmp_path, ms=80, written=["F-1", "F-2"], superseded=["F-0"])
    _extract(tmp_path, gate="reject", ms=20, written=[], superseded=[])

    g = metrics.build_metrics(root=tmp_path)["global"]
    assert g["counts"]["extract"] == 2
    assert g["extract"] == {"runs": 2, "passed": 1, "rejected": 1, "facts": 2, "superseded": 1}
    assert g["latency"]["extract"] == {"p50": 50.0, "p95": 77.0, "n": 2}


def test_recall_aggregates_ignore_null_stale(tmp_path: Path):
    _append(tmp_path, kind="recall", query="求职", top_k=5, stage=3,
            ids=["F-1", "F-2"], scores=[0.9, 0.8], stale_filtered=None, ms=30)
    _append(tmp_path, kind="recall", query="城市", top_k=3, stage=3,
            ids=["F-3"], scores=[], stale_filtered=2, ms=10)

    g = metrics.build_metrics(root=tmp_path)["global"]
    assert g["recall"]["calls"] == 2
    assert g["recall"]["results"] == 3
    assert g["recall"]["staleFiltered"] == 2      # null 不计入
    assert g["recall"]["avgTopK"] == 4.0          # (5 + 3) / 2


def test_latency_percentiles_and_n(tmp_path: Path):
    for ms in (10, 20, 30):
        _append(tmp_path, kind="brief", chars=5, facts=1, ms=ms)
    _extract(tmp_path, ms=1000)

    g = metrics.build_metrics(root=tmp_path)["global"]
    assert g["latency"]["brief"] == {"p50": 20.0, "p95": 29.0, "n": 3}
    assert g["latency"]["extract"] == {"p50": 1000.0, "p95": 1000.0, "n": 1}
    assert g["latency"]["recall"]["n"] == 0


def test_daily_and_last_brief(tmp_path: Path):
    _extract(tmp_path, written=["F-1"], days_ago=1)
    _extract(tmp_path, written=["F-2", "F-3"])
    _append(tmp_path, kind="recall", ids=["F-1"], top_k=5, days_ago=1)
    first = _append(tmp_path, kind="brief", chars=10, facts=1, ms=1, days_ago=1)
    last = _append(tmp_path, kind="brief", chars=20, facts=2, ms=2)

    g = metrics.build_metrics(root=tmp_path)["global"]
    by_date = {row["date"]: row for row in g["daily"]}
    assert by_date[date.today().isoformat()] == {
        "date": date.today().isoformat(), "extracts": 1, "facts": 2, "recalls": 0,
    }
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert by_date[yesterday]["extracts"] == 1 and by_date[yesterday]["recalls"] == 1
    assert g["lastBrief"] == {"chars": 20, "at": last["t"]}
    assert first["chars"] == 10                     # 旧 brief 被覆盖而非累加


def test_window_filters_old_events(tmp_path: Path):
    _extract(tmp_path, written=["F-new"], ms=10)
    _extract(tmp_path, written=["F-old"], ms=10, days_ago=10)

    week = metrics.build_metrics(root=tmp_path, days=7)
    assert week["global"]["counts"]["extract"] == 1
    assert week["global"]["extract"]["facts"] == 1

    month = metrics.build_metrics(root=tmp_path, days=30)
    assert month["global"]["counts"]["extract"] == 2
    assert month["global"]["extract"]["facts"] == 2


def test_events_without_valid_t_are_dropped(tmp_path: Path):
    _append(tmp_path, kind="extract", gate="pass", written=[])
    path = telemetry.events_path(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "extract", "gate": "pass", "written": []}) + "\n")
        fh.write(json.dumps({"t": "不是时间", "kind": "extract", "gate": "pass"}) + "\n")

    g = metrics.build_metrics(root=tmp_path)["global"]
    assert g["counts"]["extract"] == 1


# ---------------------------------------------------------------- session


def test_session_filter_and_summary(tmp_path: Path):
    _extract(tmp_path, written=["F-1"], session="s1")
    _append(tmp_path, kind="recall", ids=["F-1"], top_k=5, stale_filtered=None, ms=5,
            session="s1")
    _extract(tmp_path, written=["F-2"], session="s2")

    payload = metrics.build_metrics(root=tmp_path, session="s1")
    assert payload["global"]["counts"]["extract"] == 2         # global 不按 session 收窄
    assert payload["global"]["counts"]["recall"] == 1

    session = payload["session"]
    assert set(session) == {"counts", "extract", "recall", "latency", "daily", "lastBrief"}
    assert "vault" not in session
    assert session["counts"]["extract"] == 1
    assert session["counts"]["recall"] == 1
    assert session["extract"]["facts"] == 1
    assert session["latency"]["recall"]["n"] == 1
    assert len(session["daily"]) == 7

    assert [item["session"] for item in payload["recent"]] == ["s1", "s1"]
    assert all(item["kind"] in {"extract", "recall"} for item in payload["recent"])


def test_session_without_events_is_zeroed_object(tmp_path: Path):
    _extract(tmp_path, written=["F-1"], session="s1")

    payload = metrics.build_metrics(root=tmp_path, session="ghost")
    assert isinstance(payload["session"], dict)
    assert payload["session"]["counts"] == {"extract": 0, "recall": 0, "expand": 0,
                                            "brief": 0, "rebuild": 0}
    assert payload["session"]["lastBrief"] is None
    assert payload["recent"] == []


# ---------------------------------------------------------------- recent


def test_recent_limit_and_order(tmp_path: Path):
    for index in range(35):
        _extract(tmp_path, written=[f"F-{index}"], ms=index, session="s1")

    recent = metrics.build_metrics(root=tmp_path, session="s1")["recent"]
    assert len(recent) == 30
    assert recent[0]["facts"] == ["F-34"]           # 新 → 旧
    assert recent[-1]["facts"] == ["F-5"]
    assert set(recent[0]) == {"t", "kind", "session", "summary", "facts", "ms", "detail"}


def test_recent_summaries_are_chinese(tmp_path: Path):
    _extract(tmp_path, written=["F-1", "F-2"], superseded=["F-0"])
    _extract(tmp_path, gate="reject")
    _append(tmp_path, kind="recall", ids=["F-1"], stale_filtered=1, top_k=5)
    _append(tmp_path, kind="expand", fact_id="F-1", hops=1, ids=["F-2", "F-3"])
    _append(tmp_path, kind="brief", chars=88, facts=2)
    _append(tmp_path, kind="rebuild", facts=3, ok=True)

    recent = metrics.build_metrics(root=tmp_path)["recent"]
    summaries = [item["summary"] for item in reversed(recent)]   # 回到写入顺序
    assert summaries == [
        "写入 2 条，取代 1 条",
        "gate 拒绝，未写入",
        "召回 1 条，过滤过期 1 条",
        "扩散 2 条",
        "生成摘要 88 字符",
        "重建完成",
    ]
    assert recent[0]["kind"] == "rebuild" and recent[0]["summary"] == "重建完成"
    assert recent[0]["facts"] == []


# ---------------------------------------------------------------- vault 体量


def test_vault_stats_from_journal_and_views(tmp_path: Path):
    journal = Journal(tmp_path / "journal")
    journal.append(_make_fact("F-old", obj="北京/西安未定"))
    journal.append(_make_fact("F-new", obj="北京"))
    journal.supersede("F-old", "F-new")

    entities = tmp_path / "vault_views" / "entities"
    topics = tmp_path / "vault_views" / "topics"
    entities.mkdir(parents=True)
    topics.mkdir(parents=True)
    (entities / "梁明晗.md").write_text("# 梁明晗", encoding="utf-8")
    (entities / "连溯.md").write_text("# 连溯", encoding="utf-8")
    (topics / "求职.md").write_text("# 求职", encoding="utf-8")

    vault = metrics.build_metrics(root=tmp_path)["vault"]
    assert vault["facts"] == 2
    assert vault["stale"] == 1
    assert vault["entities"] == 2
    assert vault["topics"] == 1
    assert vault["journalBytes"] > 0


def test_vault_stats_broken_journal_returns_zeros(tmp_path: Path):
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "2026-11.md").write_text("这不是合法 YAML: [unclosed", encoding="utf-8")

    vault = metrics.build_metrics(root=tmp_path)["vault"]
    assert vault["facts"] == 0 and vault["stale"] == 0
    assert vault["entities"] == 0 and vault["topics"] == 0
    assert vault["journalBytes"] > 0


# ---------------------------------------------------------------- CLI


def test_cli_metrics_json(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("LIANSU_ROOT", str(tmp_path))
    _extract(tmp_path, written=["F-1"], session="s1")
    _append(tmp_path, kind="brief", chars=12, facts=1, ms=3, session="s1")

    code = cli.main(["metrics", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)          # stdout 必须只有一个可解析 JSON 对象

    assert code == 0
    assert captured.err == ""
    assert set(payload) == {"generatedAt", "windowDays", "global", "session", "vault", "recent"}
    assert payload["windowDays"] == 7 and payload["session"] is None
    assert payload["global"]["counts"]["extract"] == 1
    assert payload["recent"][0]["kind"] == "brief"


def test_cli_metrics_session_and_days(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("LIANSU_ROOT", str(tmp_path))
    _extract(tmp_path, written=["F-1"], session="s1")
    _extract(tmp_path, written=["F-2"], session="s2")
    _extract(tmp_path, written=["F-3"], session="s1", days_ago=20)

    code = cli.main(["metrics", "--json", "--session", "s1", "--days", "3"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["windowDays"] == 3
    assert len(payload["global"]["daily"]) == 3
    assert payload["global"]["counts"]["extract"] == 2     # 20 天前那条被窗口排除
    assert payload["session"]["counts"]["extract"] == 1
    assert payload["recent"][0]["session"] == "s1"


def test_cli_metrics_empty_root_still_valid_json(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("LIANSU_ROOT", str(tmp_path / "empty"))

    code = cli.main(["metrics", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["global"]["counts"]["extract"] == 0
    assert payload["vault"]["facts"] == 0


# ---------------------------------------------------------------- 埋点联通


def test_mcp_tools_emit_events(tmp_path: Path):
    """recall / expand / brief 的埋点真的落到 ctx["root"] 下（MCP 与 CLI 共用实现）。"""
    from liansu.extract.pipeline import Pipeline
    from liansu.index.bm25 import BM25Index
    from liansu.index.fuse import FuseIndex
    from liansu.index.graph import GraphIndex
    from liansu.mcp_server import brief_impl, expand_impl_tool, recall_impl, record_impl
    from liansu.vault.views import ViewBuilder

    from tests.fixtures.mock_index import MockIndex
    from tests.fixtures.mock_provider import MockProvider

    journal = Journal(tmp_path / "journal")
    bm25, graph, vector = BM25Index(), GraphIndex(), MockIndex()
    indexes = {"bm25": bm25, "vector": vector, "graph": graph}
    provider = MockProvider(facts=[_make_fact("F-1")])
    ctx = {
        "journal": journal,
        "pipeline": Pipeline(journal, provider, indexes=[bm25, graph],
                             state_path=tmp_path / "p.json"),
        "indexes": indexes,
        "fuse": FuseIndex(journal, indexes),
        "graph": graph,
        "views": ViewBuilder(tmp_path / "vault_views", journal),
        "root": tmp_path,
        "top_k": 5,
        "config": {},
    }

    dialog = json.dumps([{"role": "user", "content": "我决定求职目标城市定北京，备选西安。"}],
                        ensure_ascii=False)
    assert record_impl(ctx, dialog, source="s1")["ok"]
    assert recall_impl(ctx, "求职目标城市", top_k=3)["ok"]
    assert expand_impl_tool(ctx, "F-1", hops=1)["ok"]
    assert brief_impl(ctx, max_tokens=500)["ok"]

    events = telemetry.read_events(tmp_path)
    kinds = [event["kind"] for event in events]
    assert kinds == ["extract", "recall", "expand", "brief"]

    extract, recall, expand, brief = events
    assert extract["gate"] == "pass" and extract["written"] == ["F-1"]
    assert extract["model"] is None and extract["chars"] > 0 and extract["ms"] >= 0
    assert recall["ids"] == ["F-1"] and recall["top_k"] == 3 and recall["stage"] == 3
    assert len(recall["scores"]) == len(recall["ids"])
    assert recall["stale_filtered"] is None
    assert expand["fact_id"] == "F-1" and expand["hops"] == 1
    assert brief["chars"] >= 0 and isinstance(brief["facts"], int)


def test_recent_detail_for_extract_and_recall(tmp_path: Path):
    """recent 必须带可展开明细：extract 给 gate/写入/取代，recall 给查询/命中/分数。"""
    _append(tmp_path, kind="extract", gate="pass", written=["F-1", "F-2"],
            superseded=["F-0"], chars=120, model="local", ms=3456.7)
    _append(
        tmp_path, kind="recall", query="目标城市定了吗", top_k=5, stage=3,
        ids=["F-1", "F-2"], scores=[0.0164, 0.0159], stale_filtered=2, ms=12.5,
    )

    recent = metrics.build_metrics(root=tmp_path)["recent"]
    recall, extract = recent[0]["detail"], recent[1]["detail"]

    assert recall["query"] == "目标城市定了吗"
    assert recall["topK"] == 5 and recall["stage"] == 3
    assert recall["ids"] == ["F-1", "F-2"]
    assert recall["scores"] == [0.0164, 0.0159]
    assert recall["staleFiltered"] == 2.0

    assert extract["gate"] == "pass"
    assert extract["written"] == ["F-1", "F-2"]
    assert extract["superseded"] == ["F-0"]
    assert extract["chars"] == 120.0
    assert extract["model"] == "local"
    assert recent[0]["ms"] == 12.5


def test_recent_detail_tolerates_missing_fields(tmp_path: Path):
    """事件字段缺失/类型不对时明细退化为 None / []，不能让插件拿到 undefined。"""
    _append(tmp_path, kind="recall", ids="not-a-list", scores=[None, 1.0], top_k="5")
    detail = metrics.build_metrics(root=tmp_path)["recent"][0]["detail"]
    assert detail["ids"] == []
    assert detail["scores"] == [1.0]
    assert detail["topK"] is None
    assert detail["query"] is None


def test_recent_detail_carries_fact_bodies(tmp_path: Path):
    """展开明细要看到记忆原文：statement / 三元组 / 有效期 / 来源，而不是只有 id。"""
    journal = Journal(tmp_path / "journal")
    journal.append(_make_fact("F-new", obj="北京"))
    journal.append(_make_fact("F-old", obj="北京/西安未定"))
    journal.supersede("F-old", "F-new")

    _append(tmp_path, kind="extract", gate="pass", written=["F-new"],
            superseded=["F-old"], chars=120, model="local")
    _append(tmp_path, kind="recall", query="目标城市", top_k=5, stage=3,
            ids=["F-new", "F-missing"], scores=[0.0164, 0.01])

    recent = metrics.build_metrics(root=tmp_path)["recent"]
    recall, extract = recent[0]["detail"], recent[1]["detail"]

    assert extract["writtenFacts"]["items"][0]["statement"] == "梁明晗求职目标城市北京"
    assert extract["writtenFacts"]["items"][0]["validFrom"] == date.today().isoformat()
    assert extract["supersededFacts"]["items"][0]["validTo"] == date.today().isoformat()

    assert recall["facts"]["items"][0]["statement"] == "梁明晗求职目标城市北京"
    assert recall["facts"]["items"][0]["score"] == 0.0164
    # journal 里查不到的 id 不能吞掉：标 found=false，前端提示「未找到」
    assert recall["facts"]["items"][1] == {"id": "F-missing", "found": False, "score": 0.01}
    assert recall["facts"]["total"] == 2 and recall["facts"]["omitted"] == 0


def test_recent_detail_fact_bodies_are_capped(tmp_path: Path):
    """单条事件最多带 DETAIL_FACT_LIMIT 条正文，其余用 omitted 计数，避免 recent 无限膨胀。"""
    journal = Journal(tmp_path / "journal")
    ids = [f"F-{index}" for index in range(15)]
    for fid in ids:
        journal.append(_make_fact(fid, obj=f"城市{fid}"))
    _append(tmp_path, kind="recall", query="城市", ids=ids, scores=[0.01] * len(ids))

    block = metrics.build_metrics(root=tmp_path)["recent"][0]["detail"]["facts"]
    assert len(block["items"]) == metrics.DETAIL_FACT_LIMIT
    assert block["total"] == 15 and block["omitted"] == 5
