"""T17 · 服务可用性自检的契约测试。

覆盖：健康 vault 可用、缺目录不可用、缺可选项降级、LLM 探测失败降级、CLI JSON 与退出码。
"""
from __future__ import annotations

import json

from liansu import cli
from liansu.health import check_service


def _healthy_root(tmp_path):
    root = tmp_path / "vault"
    (root / "journal").mkdir(parents=True)
    (root / "journal" / "2026-11.md").write_text("# 2026-11\n", encoding="utf-8")
    (root / ".liansu").mkdir()
    (root / ".liansu" / "index.db").write_bytes(b"sqlite")
    (root / "vault_views").mkdir()
    (root / "vault_views" / "brief.md").write_text("# 记忆摘要\n", encoding="utf-8")
    return root


def _by_id(payload, ident):
    for item in payload["checks"]:
        if item["id"] == ident:
            return item
    raise AssertionError(f"missing check {ident}")


def test_healthy_vault_is_available(tmp_path, monkeypatch):
    monkeypatch.setattr("liansu.health._embedder_cached", lambda model: True)
    payload = check_service(_healthy_root(tmp_path), probe_llm=False)

    assert payload["available"] is True
    assert payload["degraded"] is False
    assert payload["reason"] == ""
    assert payload["version"]
    assert payload["python"]
    for ident in ("root", "config", "journal", "index", "views", "llm", "embedder"):
        _by_id(payload, ident)
    assert _by_id(payload, "root")["level"] == "required"
    assert _by_id(payload, "index")["level"] == "optional"

    caps = payload["capabilities"]
    assert set(caps) == {"extract", "recall", "inject"}
    assert caps["extract"]["ok"] is True
    assert caps["extract"]["requires"] == ["llm", "journal"]
    assert caps["recall"]["ok"] is True
    assert caps["inject"]["ok"] is True


def test_missing_root_is_unavailable(tmp_path):
    payload = check_service(tmp_path / "nope", probe_llm=False)

    assert payload["available"] is False
    assert "根目录" in payload["reason"]
    assert _by_id(payload, "root")["ok"] is False
    for capability in payload["capabilities"].values():
        assert capability["ok"] is False


def test_missing_optional_checks_degrade(tmp_path, monkeypatch):
    monkeypatch.setattr("liansu.health._embedder_cached", lambda model: False)
    root = tmp_path / "vault"
    (root / "journal").mkdir(parents=True)

    payload = check_service(root, probe_llm=False)

    assert payload["available"] is True
    assert payload["degraded"] is True
    assert payload["reason"]


def test_llm_probe_failure_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr("liansu.health._embedder_cached", lambda model: True)
    monkeypatch.setattr("liansu.health._llm_check", lambda cfg, timeout, probe: (False, "LLM 不可达：测试端点"))

    payload = check_service(_healthy_root(tmp_path), probe_llm=True)

    assert payload["available"] is True
    assert payload["degraded"] is True
    llm = _by_id(payload, "llm")
    assert llm["ok"] is False
    assert llm["level"] == "optional"
    assert "LLM 不可达" in llm["detail"]

    # LLM 挂了只挡抽取；检索与注入照常。
    caps = payload["capabilities"]
    assert caps["extract"]["ok"] is False
    assert "LLM 不可达" in caps["extract"]["reason"]
    assert caps["recall"]["ok"] is True
    assert caps["inject"]["ok"] is True


def test_llm_check_reports_resolver_failure(tmp_path, monkeypatch):
    from liansu.deskflow import DeskflowResolveError
    from liansu.health import _llm_check

    def boom(self):
        raise DeskflowResolveError("候选地址均不可用")

    monkeypatch.setattr("liansu.deskflow.DeskflowEndpointResolver.current_base_url", boom)
    ok, detail = _llm_check({"llm": {"base_url": "http://127.0.0.1:1/v1", "model": "local"}}, timeout=0.2, probe=True)
    assert ok is False
    assert "解析 LLM 端点失败" in detail


def test_checks_carry_ms_and_label(tmp_path, monkeypatch):
    monkeypatch.setattr("liansu.health._embedder_cached", lambda model: True)
    payload = check_service(_healthy_root(tmp_path), probe_llm=False)

    for item in payload["checks"]:
        assert set(item) == {"id", "label", "ok", "level", "detail", "ms"}
        assert isinstance(item["ms"], float)
        assert item["level"] in ("required", "optional")


def test_cli_health_json_and_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LIANSU_ROOT", str(_healthy_root(tmp_path)))
    monkeypatch.setattr("liansu.health._embedder_cached", lambda model: True)

    assert cli.main(["health", "--json", "--no-llm"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["available"] is True


def test_cli_health_exits_one_when_unavailable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LIANSU_ROOT", str(tmp_path / "missing"))

    assert cli.main(["health", "--json", "--no-llm"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["available"] is False
