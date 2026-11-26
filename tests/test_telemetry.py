"""T16 telemetry：JSONL 事件流契约、禁用开关、坏行、轮转、显式 vault 根。"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

import pytest

from liansu import telemetry


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """默认让每个用例从「开启、无 session、无 LIANSU_ROOT」的干净环境开始。"""
    monkeypatch.delenv("LIANSU_TELEMETRY", raising=False)
    monkeypatch.delenv("LIANSU_SESSION", raising=False)
    monkeypatch.delenv("LIANSU_ROOT", raising=False)


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_record_writes_contract_line(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIANSU_SESSION", "s1")
    telemetry.record_at(tmp_path, "recall", query="求职目标城市", top_k=5, ids=["F-1"], ms=12.5)

    path = tmp_path / ".liansu" / "metrics" / "events.jsonl"
    assert path.exists()
    raw = _lines(path)
    assert len(raw) == 1
    event = json.loads(raw[0])

    assert event["kind"] == "recall"
    assert event["session"] == "s1"
    assert event["pid"] == os.getpid()
    assert event["v"] == 1
    assert event["query"] == "求职目标城市" and event["top_k"] == 5
    # t 必须是带时区偏移的 ISO8601
    parsed = datetime.fromisoformat(event["t"])
    assert parsed.tzinfo is not None and parsed.utcoffset() is not None


def test_session_null_when_env_missing(tmp_path: Path):
    telemetry.record_at(tmp_path, "extract", gate="pass")
    event = telemetry.read_events(tmp_path)[0]
    assert event["session"] is None


def test_record_uses_liansu_root_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIANSU_ROOT", str(tmp_path))
    telemetry.record("brief", chars=42)

    event = telemetry.read_events(tmp_path)[0]
    assert event["kind"] == "brief" and event["chars"] == 42


def test_disabled_switch_is_noop(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIANSU_TELEMETRY", "0")
    telemetry.record_at(tmp_path, "extract", gate="pass")
    telemetry.record_at(tmp_path, "recall", ids=[])

    assert not (tmp_path / ".liansu" / "metrics" / "events.jsonl").exists()
    assert telemetry.read_events(tmp_path) == []


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_disabled_values(value: str, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIANSU_TELEMETRY", value)
    assert telemetry.enabled() is False
    telemetry.record_at(tmp_path, "extract")
    assert telemetry.read_events(tmp_path) == []


def test_enabled_by_default_and_for_other_values(tmp_path: Path):
    assert telemetry.enabled() is True


def test_read_events_ignores_bad_lines(tmp_path: Path):
    path = telemetry.events_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"t": "2026-11-25T10:00:00+08:00", "kind": "extract", "v": 1})
    path.write_text(
        "\n".join([good, "{不是 JSON", "", "[1, 2, 3]", '"字符串"', good[:20]]) + "\n",
        encoding="utf-8",
    )

    events = telemetry.read_events(tmp_path)
    assert len(events) == 1 and events[0]["kind"] == "extract"
    assert telemetry.tail(events, 5)[0]["kind"] == "extract"


def test_read_events_missing_file(tmp_path: Path):
    assert telemetry.read_events(tmp_path / "nope") == []


def test_rotation_threshold(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(telemetry, "MAX_BYTES", 10)
    telemetry.record_at(tmp_path, "extract", gate="pass", written=["F-1"])
    metrics = telemetry.metrics_dir(tmp_path)
    assert (metrics / "events.jsonl").stat().st_size > 10

    telemetry.record_at(tmp_path, "extract", gate="reject", written=[])

    rotated = metrics / "events.1.jsonl"
    assert rotated.exists()
    assert len(telemetry.read_events(tmp_path)) == 1
    assert len(_lines(rotated)) == 1


def test_rotation_only_when_over_threshold(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(telemetry, "MAX_BYTES", 5 * 1024 * 1024)
    telemetry.record_at(tmp_path, "brief", chars=1)
    telemetry.record_at(tmp_path, "brief", chars=2)
    assert not (telemetry.metrics_dir(tmp_path) / "events.1.jsonl").exists()
    assert len(telemetry.read_events(tmp_path)) == 2


def test_io_error_is_swallowed(tmp_path: Path, monkeypatch, capsys):
    # 让 metrics 目录的父路径是一个普通文件 -> mkdir 必然失败
    blocker = tmp_path / ".liansu"
    blocker.write_text("not a directory", encoding="utf-8")

    telemetry.record_at(tmp_path, "extract", gate="pass")

    captured = capsys.readouterr()
    assert "telemetry" in captured.err
    assert captured.err.count("\n") == 1
    assert not (tmp_path / ".liansu" / "metrics").exists()


def test_record_spawns_directory(tmp_path: Path):
    target = tmp_path / "deep" / "vault"
    telemetry.record_at(target, "rebuild", facts=3, ok=True)
    assert (target / ".liansu" / "metrics" / "events.jsonl").exists()
    assert telemetry.read_events(target)[0]["facts"] == 3


def test_fields_flattened_to_top_level(tmp_path: Path):
    telemetry.record_at(tmp_path, "expand", fact_id="F-1", hops=2, ids=["F-2"], ms=0.5)
    event = telemetry.read_events(tmp_path)[0]
    for key in ("fact_id", "hops", "ids", "ms"):
        assert key in event
    assert event["ids"] == ["F-2"] and event["ms"] == 0.5


def test_record_is_positional_only_kind(tmp_path: Path):
    with pytest.raises(TypeError):
        telemetry.record(kind="extract")  # kind 必须是 positional-only


def test_concurrent_writes_are_intact(tmp_path: Path):
    import threading

    def worker(index: int) -> None:
        for n in range(5):
            telemetry.record_at(tmp_path, "recall", query=f"q{index}-{n}", ids=[f"F-{index}-{n}"],
                                top_k=1, ms=1.0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = telemetry.read_events(tmp_path)
    assert len(events) == 40                     # 一行一条，无交错半行
    assert len({event["ids"][0] for event in events}) == 40


def test_t_has_local_offset_shape(tmp_path: Path):
    telemetry.record_at(tmp_path, "brief", chars=0)
    t = telemetry.read_events(tmp_path)[0]["t"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", t)
