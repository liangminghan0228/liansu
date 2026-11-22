"""T3 会话捕获测试：全部内存/临时文件 mock，不依赖真实 Codex 会话。"""
from __future__ import annotations

import io
import json
import os
import sys

import zstandard

from liansu.capture.hook import (
    CODE_NO_ROLLOUT,
    CODE_PARSE_ERROR,
    capture,
)
from liansu.capture.parse import parse_active, parse_zst
from liansu.cli import main


def _msg_line(role: str, text: str) -> str:
    """构造一条 Codex response_item message 行。"""
    text_type = "input_text" if role == "user" else "output_text"
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": role,
                "content": [{"type": text_type, "text": text}],
            },
        },
        ensure_ascii=False,
    )


def _write_zst(path, lines: list[str], mtime: float | None = None) -> None:
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    path.write_bytes(zstandard.ZstdCompressor().compress(raw))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _sample_lines() -> list[str]:
    return [
        json.dumps({"type": "session_meta", "payload": {"session_id": "x"}}, ensure_ascii=False),
        _msg_line("user", "我决定求职目标城市就定北京了"),
        json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}, ensure_ascii=False),
        _msg_line("assistant", "好的，已记下：北京"),
        "{这不是合法 json，应当被跳过",
        _msg_line("user", "身高188体重82"),
    ]


# ---------- parse ----------

def test_parse_zst_extracts_turns_and_skips_bad_lines(tmp_path):
    path = tmp_path / "rollout.jsonl.zst"
    _write_zst(path, _sample_lines())

    turns = parse_zst(path)

    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[0] == {"role": "user", "content": "我决定求职目标城市就定北京了"}
    assert turns[2]["content"] == "身高188体重82"


def test_parse_active_uncompressed(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(_sample_lines()), encoding="utf-8")

    turns = parse_active(path)

    assert [t["role"] for t in turns] == ["user", "assistant", "user"]


def test_parse_filters_system_injection(tmp_path):
    path = tmp_path / "rollout.jsonl"
    lines = [
        _msg_line("user", "<environment_context><cwd>/x</cwd></environment_context>我的真实问题"),
        _msg_line("user", "# AGENTS.md instructions for /x\n\nremember to be nice"),
        _msg_line("developer", "system prompt should be ignored"),
        _msg_line("assistant", "这是回答"),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    turns = parse_active(path)

    assert turns == [
        {"role": "user", "content": "我的真实问题"},
        {"role": "assistant", "content": "这是回答"},
    ]


def test_parse_active_handles_zst_suffix(tmp_path):
    """parse_active 对 .zst 后缀也应正确解压（sessions 下存在混合后缀）。"""
    path = tmp_path / "rollout.jsonl.zst"
    _write_zst(path, [_msg_line("user", "压缩后缀兼容")])

    assert parse_active(path) == [{"role": "user", "content": "压缩后缀兼容"}]


# ---------- hook ----------

def test_capture_stdin_takes_priority(tmp_path):
    """stdin 有对话时不得走降级：归档目录指向不存在的路径仍应成功。"""
    missing = tmp_path / "does-not-exist"
    result = capture(
        _msg_line("user", "来自 stdin 的内容"),
        archived_dir=missing,
        sessions_dir=missing,
    )

    assert result["ok"] is True
    assert result["code"] == "OK"
    assert result["source"].startswith("stdin:")
    assert result["turns"] == [{"role": "user", "content": "来自 stdin 的内容"}]


def test_capture_stdin_json_payload_with_turns(tmp_path):
    payload = json.dumps(
        {
            "cwd": "/tmp",
            "stop_hook_active": False,
            "turns": [
                {"role": "user", "content": "记住我在北京"},
                {"role": "assistant", "content": "已记下"},
            ],
        },
        ensure_ascii=False,
    )
    missing = tmp_path / "missing"
    result = capture(payload, archived_dir=missing, sessions_dir=missing)

    assert result["ok"] is True
    assert result["source"] == "stdin:field:turns"
    assert len(result["turns"]) == 2


def test_capture_fallback_to_latest_archive(tmp_path):
    archived = tmp_path / "archived"
    archived.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    old = archived / "old.jsonl.zst"
    latest = archived / "latest.jsonl.zst"
    _write_zst(old, [_msg_line("user", "旧归档")], mtime=1000)
    _write_zst(latest, [_msg_line("user", "最新归档")], mtime=2000)

    result = capture(None, archived_dir=archived, sessions_dir=sessions)

    assert result["ok"] is True
    assert result["source"] == str(latest)
    assert result["turns"] == [{"role": "user", "content": "最新归档"}]


def test_capture_fallback_skips_corrupt_and_tries_next(tmp_path):
    archived = tmp_path / "archived"
    archived.mkdir()
    corrupt = archived / "corrupt.jsonl.zst"
    corrupt.write_bytes(b"not a zstd stream")
    os.utime(corrupt, (2000, 2000))
    good = archived / "good.jsonl.zst"
    _write_zst(good, [_msg_line("assistant", "好的候选")], mtime=1000)

    result = capture(None, archived_dir=archived, sessions_dir=tmp_path / "sessions")

    assert result["ok"] is True
    assert result["source"] == str(good)
    assert result["turns"][0]["content"] == "好的候选"


def test_capture_returns_no_rollout_when_no_files(tmp_path):
    result = capture(
        None,
        archived_dir=tmp_path / "empty_archived",
        sessions_dir=tmp_path / "empty_sessions",
    )

    assert result["ok"] is False
    assert result["code"] == CODE_NO_ROLLOUT
    assert result["turns"] == []
    assert result["error"]


def test_capture_returns_parse_error_on_all_corrupt(tmp_path):
    archived = tmp_path / "archived"
    archived.mkdir()
    (archived / "bad.jsonl.zst").write_bytes(b"broken")

    result = capture(None, archived_dir=archived, sessions_dir=tmp_path / "sessions")

    assert result["ok"] is False
    assert result["code"] == CODE_PARSE_ERROR
    assert result["error"]


def test_capture_source_path_option(tmp_path):
    src = tmp_path / "rollout.jsonl"
    src.write_text(_msg_line("user", "指定文件"), encoding="utf-8")

    result = capture(None, source_path=src, archived_dir=tmp_path / "x")

    assert result["ok"] is True
    assert result["source"] == str(src)
    assert result["turns"][0]["content"] == "指定文件"


# ---------- cli ----------

def test_cli_capture_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(_msg_line("user", "cli stdin")))

    rc = main(["capture", "--stdin"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["turns"][0]["content"] == "cli stdin"


def test_cli_capture_source_and_out(tmp_path):
    src = tmp_path / "rollout.jsonl"
    src.write_text(_msg_line("assistant", "写文件"), encoding="utf-8")
    out = tmp_path / "result.json"

    rc = main(["capture", "--source", str(src), "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is True


def test_cli_placeholders(capsys):
    assert main(["record"]) == 0
    assert main(["recall"]) == 0
    assert main(["rebuild"]) == 0
    assert "not implemented" in capsys.readouterr().out
