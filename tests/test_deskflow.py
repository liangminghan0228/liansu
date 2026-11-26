"""Deskflow 动态端点解析单测：不发真实网络请求。"""
from __future__ import annotations

import json
import os
import plistlib
import time
from unittest.mock import MagicMock, patch

import pytest
from openai import APIConnectionError

from liansu.deskflow import (
    DeskflowEndpointResolver,
    DeskflowResolveError,
    health_url,
    read_live_peers,
    read_remote_hosts,
)
from liansu.providers.api import ApiProvider

TEMPLATE = "http://{deskflow_host}:8080/v1"


def _write_conf(path, host_line: str) -> None:
    path.write_text(f"[client]\nremoteHost={host_line}\n", encoding="utf-8")
    now = time.time_ns() + 1_000_000
    os.utime(path, ns=(now, now))


def test_read_remote_hosts_from_ini(tmp_path):
    conf = tmp_path / "Deskflow.conf"
    _write_conf(conf, "192.168.0.101:24800, 192.168.0.102")

    hosts, used = read_remote_hosts(conf)

    assert hosts == ["192.168.0.101", "192.168.0.102"]
    assert used == conf


def test_read_remote_hosts_from_plist(tmp_path):
    conf = tmp_path / "Deskflow.plist"
    with conf.open("wb") as fh:
        plistlib.dump({"client": {"remoteHost": "10.0.0.7"}}, fh)

    hosts, used = read_remote_hosts(conf)

    assert hosts == ["10.0.0.7"]
    assert used == conf


def test_read_live_peers_parses_lsof():
    fake = MagicMock()
    fake.stdout = (
        "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
        "deskflow 52827 liangminghan 14u IPv4 0x1 0t0 TCP "
        "192.168.0.104:24800->192.168.0.101:14383 (ESTABLISHED)\n"
        "deskflow 52827 liangminghan 15u IPv4 0x2 0t0 TCP "
        "192.168.0.104:24800->127.0.0.1:5555 (ESTABLISHED)\n"
    )
    with patch("liansu.deskflow.shutil.which", return_value="/usr/sbin/lsof"), \
         patch("liansu.deskflow.subprocess.run", return_value=fake) as run:
        assert read_live_peers(24800) == ["192.168.0.101"]

    assert run.call_args.args[0] == [
        "/usr/sbin/lsof", "-nP", "-iTCP:24800", "-sTCP:ESTABLISHED"
    ]


def test_resolver_prefers_live_peer(tmp_path):
    conf = tmp_path / "Deskflow.conf"
    _write_conf(conf, "10.0.0.9")
    llm = {
        "base_url": TEMPLATE,
        "deskflow": {
            "source": "auto",
            "config_path": str(conf),
            "fallback_host": "10.0.0.99",
        },
    }

    with patch("liansu.deskflow.read_live_peers", return_value=["10.0.0.1"]), \
         patch("liansu.deskflow.probe_health", return_value=True):
        resolver = DeskflowEndpointResolver(llm)
        assert resolver.current_base_url() == "http://10.0.0.1:8080/v1"
        assert resolver.last_source == "live"


def test_resolver_tracks_live_peer_change(tmp_path):
    llm = {"base_url": TEMPLATE, "deskflow": {"source": "live", "cache_ttl": 60}}

    with patch("liansu.deskflow.read_live_peers") as live, \
         patch("liansu.deskflow.probe_health", return_value=True):
        resolver = DeskflowEndpointResolver(llm)
        live.return_value = ["10.0.0.1"]
        assert resolver.current_base_url() == "http://10.0.0.1:8080/v1"
        live.return_value = ["10.0.0.2"]
        assert resolver.current_base_url() == "http://10.0.0.2:8080/v1"
        assert live.call_count == 2  # 每次调用都实时读，不靠 TTL


def test_resolver_source_config_ignores_live(tmp_path):
    conf = tmp_path / "Deskflow.conf"
    _write_conf(conf, "10.0.0.9")
    llm = {"base_url": TEMPLATE, "deskflow": {"source": "config", "config_path": str(conf)}}

    with patch("liansu.deskflow.read_live_peers", return_value=["10.0.0.1"]), \
         patch("liansu.deskflow.probe_health", return_value=True):
        assert DeskflowEndpointResolver(llm).current_base_url() == "http://10.0.0.9:8080/v1"


def test_resolver_probes_candidates_and_invalidates_on_mtime(tmp_path):
    conf = tmp_path / "Deskflow.conf"
    _write_conf(conf, "10.0.0.9")
    llm = {
        "base_url": TEMPLATE,
        "deskflow": {"source": "config", "config_path": str(conf), "cache_ttl": 60},
    }

    with patch("liansu.deskflow.probe_health", return_value=True) as probe:
        resolver = DeskflowEndpointResolver(llm)
        assert resolver.current_base_url() == "http://10.0.0.9:8080/v1"
        assert resolver.current_base_url() == "http://10.0.0.9:8080/v1"
        assert probe.call_count == 1  # TTL 缓存命中
        assert probe.call_args.args[0] == "http://10.0.0.9:8080/health"

        _write_conf(conf, "10.0.0.10")
        assert resolver.current_base_url() == "http://10.0.0.10:8080/v1"
        assert probe.call_count == 2  # mtime 变化立即失效


def test_resolver_skips_dead_candidate(tmp_path):
    conf = tmp_path / "Deskflow.conf"
    _write_conf(conf, "10.0.0.1, 10.0.0.2")
    llm = {
        "base_url": TEMPLATE,
        "deskflow": {"source": "config", "config_path": str(conf)},
    }

    def fake_probe(url, timeout):
        return "10.0.0.2" in url

    with patch("liansu.deskflow.probe_health", side_effect=fake_probe):
        assert DeskflowEndpointResolver(llm).current_base_url() == "http://10.0.0.2:8080/v1"


def test_resolver_raises_when_all_candidates_dead(tmp_path):
    conf = tmp_path / "Deskflow.conf"
    _write_conf(conf, "10.0.0.1")
    llm = {
        "base_url": TEMPLATE,
        "deskflow": {"source": "config", "config_path": str(conf)},
    }

    with patch("liansu.deskflow.probe_health", return_value=False):
        with pytest.raises(DeskflowResolveError, match="均不可用"):
            DeskflowEndpointResolver(llm).current_base_url()


def test_health_url_strips_v1():
    assert health_url("http://10.0.0.1:8080/v1") == "http://10.0.0.1:8080/health"


def _provider_cfg(conf) -> dict:
    return {
        "llm": {
            "base_url": TEMPLATE,
            "api_key": "dummy",
            "model": "local",
            "deskflow": {"source": "config", "config_path": str(conf)},
        }
    }


def _gate_client(*contents: str) -> MagicMock:
    client = MagicMock()
    responses = []
    for content in contents:
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content=content))]
        responses.append(resp)
    client.chat.completions.create.side_effect = responses
    return client


def test_provider_syncs_client_on_config_change(tmp_path):
    conf = tmp_path / "Deskflow.conf"
    _write_conf(conf, "10.0.0.1")
    client = _gate_client(json.dumps({"category": "长期事实", "worth_remembering": True, "reason": "ok"}))

    with patch("liansu.deskflow.probe_health", return_value=True), \
         patch("liansu.providers.api.OpenAI", return_value=client) as ctor:
        provider = ApiProvider(_provider_cfg(conf))
        assert provider.base_url == "http://10.0.0.1:8080/v1"
        _write_conf(conf, "10.0.0.2")
        assert provider.gate([{"role": "user", "content": "我决定目标城市定在北京"}]) is True

    assert provider.base_url == "http://10.0.0.2:8080/v1"
    assert ctor.call_count == 2


def test_provider_force_refreshes_after_connection_error(tmp_path):
    conf = tmp_path / "Deskflow.conf"
    _write_conf(conf, "10.0.0.1")
    ok_resp = MagicMock()
    ok_resp.choices = [MagicMock(message=MagicMock(content=json.dumps(
        {"category": "长期事实", "worth_remembering": True, "reason": "ok"}
    )))]
    client = MagicMock()
    client.chat.completions.create.side_effect = [APIConnectionError(request=MagicMock()), ok_resp]

    with patch("liansu.deskflow.probe_health", return_value=True), \
         patch("liansu.providers.api.OpenAI", return_value=client) as ctor:
        provider = ApiProvider(_provider_cfg(conf))
        old, new = "http://10.0.0.1:8080/v1", "http://10.0.0.2:8080/v1"
        with patch.object(
            provider._resolver,
            "current_base_url",
            side_effect=[old, new, new],
        ) as resolve:
            assert provider.gate([{"role": "user", "content": "我决定目标城市定在北京"}]) is True

    assert provider.base_url == "http://10.0.0.2:8080/v1"
    assert ctor.call_args_list[1].kwargs["base_url"] == "http://10.0.0.2:8080/v1"
    assert resolve.call_args_list[1].kwargs == {"force": True}
