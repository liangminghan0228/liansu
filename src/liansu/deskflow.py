"""Deskflow 驱动的动态 LLM 端点解析。

优先读取本机 Deskflow 服务端已建立的 TCP 连接对端 IP（Windows 客户端）。
Windows 服务/Deskflow 每次启动重连后，对端 IP 即为 DHCP 最新地址，liansu 每次调用接口
都能实时拿到，不依赖静态配置。若实时连接不可用，再退回 `[client] remoteHost` 与 fallback_host。
"""
from __future__ import annotations

import configparser
import ipaddress
import logging
import os
import plistlib
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

log = logging.getLogger(__name__)

HOST_PLACEHOLDER = "{deskflow_host}"
DEFAULT_PROBE_TIMEOUT = 2.0
DEFAULT_CACHE_TTL = 60.0
DEFAULT_DESKFLOW_PORT = 24800

_CONFIG_ENV = "LIANSU_DESKFLOW_CONFIG"
_CONFIG_CANDIDATES = (
    "~/Library/Deskflow/Deskflow.conf",
    "~/.config/Deskflow/Deskflow.conf",
    "/etc/Deskflow/Deskflow.conf",
    "~/Library/Preferences/com.deskflow.Deskflow.plist",
)


class DeskflowResolveError(RuntimeError):
    """无法从 Deskflow 配置解析出可用的 LLM 主机地址。"""


@dataclass(frozen=True)
class ResolvedEndpoint:
    base_url: str
    host: str
    source: str


def is_dynamic_base_url(base_url: str | None) -> bool:
    return bool(base_url and HOST_PLACEHOLDER in base_url)


def _config_paths(explicit: str | Path | None = None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    env_path = os.environ.get(_CONFIG_ENV)
    if env_path:
        return [Path(env_path).expanduser()]
    return [Path(p).expanduser() for p in _CONFIG_CANDIDATES]


def _split_host(raw: str) -> str:
    """从 `host:24800` / `[IPv6]:24800` 中取出 host。"""
    value = raw.strip()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            raise ValueError(f"IPv6 地址缺少右括号: {raw}")
        return value[1:end]
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if host and port.isdigit():
            return host
    return value


def _read_remote_hosts(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw_values: list[str] = []
    if path.suffix.lower() == ".plist":
        with path.open("rb") as fh:
            data = plistlib.load(fh)
        if not isinstance(data, dict):
            return []
        for container in (data, data.get("client")):
            if not isinstance(container, dict):
                continue
            for key in ("remoteHost", "remotehost"):
                if container.get(key):
                    raw_values.append(str(container[key]))
    else:
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str.lower
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                parser.read_file(fh)
        except configparser.Error as exc:
            raise DeskflowResolveError(f"Deskflow 配置解析失败 {path}: {exc}") from exc
        for section in parser.sections():
            if section.lower() != "client":
                continue
            for key in ("remotehost",):
                value = parser.get(section, key, fallback=None)
                if value:
                    raw_values.append(value)

    hosts: list[str] = []
    for value in raw_values:
        for item in value.split(","):
            host = _split_host(item)
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def read_remote_hosts(config_path: str | Path | None = None) -> tuple[list[str], Path | None]:
    """返回 (候选主机列表, 实际使用的配置文件)。"""
    for path in _config_paths(config_path):
        hosts = _read_remote_hosts(path)
        if hosts:
            return hosts, path
    return [], None


def _lsof_peers(port: int) -> list[str]:
    """macOS：从已建立的 Deskflow TCP 连接取远端 IP。"""
    exe = shutil.which("lsof")
    if not exe:
        return []
    result = subprocess.run(
        [exe, "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
        capture_output=True, text=True, timeout=3, check=False,
    )
    peers: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        name = next((field for field in fields if "->" in field), "")
        if not name:
            continue
        remote = name.split("->", 1)[1]
        try:
            host = _split_host(remote)
        except ValueError:
            continue
        if host and host not in peers and host not in {"127.0.0.1", "::1"}:
            peers.append(host)
    return peers


def _ss_peers(port: int) -> list[str]:
    """Linux 兜底：ss 已建立的 Deskflow 连接。"""
    exe = shutil.which("ss")
    if not exe:
        return []
    result = subprocess.run(
        [exe, "-H", "-n", "-t", "state", "established",
         f"( sport = :{port} or dport = :{port} )"],
        capture_output=True, text=True, timeout=3, check=False,
    )
    peers: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        remote = fields[4]
        try:
            host = _split_host(remote)
        except ValueError:
            continue
        if host and host not in peers and host not in {"127.0.0.1", "::1"}:
            peers.append(host)
    return peers


def read_live_peers(port: int = DEFAULT_DESKFLOW_PORT) -> list[str]:
    """实时读取 Deskflow 对端 IP；本机是 client 或 server 都能拿到对端。"""
    for reader in (_lsof_peers, _ss_peers):
        try:
            peers = reader(port)
        except (OSError, subprocess.SubprocessError):
            continue
        if peers:
            return peers
    return []


def _normalize_host(host: str, *, resolve_dns: bool = True) -> str:
    stripped = host.strip()
    try:
        return str(ipaddress.ip_address(stripped))
    except ValueError:
        pass
    if resolve_dns and stripped:
        try:
            infos = socket.getaddrinfo(stripped, None)
            for info in infos:
                if info[0] == socket.AF_INET:
                    return info[4][0]
            if infos:
                return infos[0][4][0]
        except OSError:
            pass
    return stripped


def health_url(base_url: str, health_path: str = "/health") -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root.rstrip("/") + ("/" + health_path.lstrip("/") if health_path else "/health")


def probe_health(url: str, timeout: float = DEFAULT_PROBE_TIMEOUT) -> bool:
    """探测 LLM 服务健康端点；显式禁用系统代理，避免内网请求被 HTTP_PROXY 拦截。"""
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            return getattr(resp, "status", 200) == 200
    except Exception as exc:
        log.debug("Deskflow 端点探测失败 %s: %s", url, exc)
        return False


class DeskflowEndpointResolver:
    """把 Deskflow remoteHost 解析为可用的 LLM base_url。"""

    def __init__(self, llm_config: dict[str, Any] | None = None):
        llm = llm_config or {}
        self.template = str(llm.get("base_url") or "")
        self.dynamic = is_dynamic_base_url(self.template)
        opts = llm.get("deskflow") or {}
        if not isinstance(opts, dict):
            opts = {}
        self.enabled = bool(opts.get("enabled", True))
        self.source = str(opts.get("source", "auto")).lower()
        self.port = int(opts.get("port", DEFAULT_DESKFLOW_PORT))
        self.config_path = opts.get("config_path")
        self.health_path = str(opts.get("health_path", "/health"))
        self.probe = bool(opts.get("probe", True))
        self.probe_timeout = float(opts.get("probe_timeout", DEFAULT_PROBE_TIMEOUT))
        self.cache_ttl = float(opts.get("cache_ttl", DEFAULT_CACHE_TTL))
        self.fallback_host = opts.get("fallback_host")
        self._cached: ResolvedEndpoint | None = None
        self._cached_at = 0.0
        self._signature = ""

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0
        self._signature = ""

    @property
    def last_source(self) -> str:
        return self._cached.source if self._cached else ""

    def _collect_candidates(self) -> tuple[list[tuple[str, str]], str]:
        """按优先级收集 (host, source)：实时连接 > 配置 remoteHost > fallback。"""
        candidates: list[tuple[str, str]] = []
        if self.source in ("auto", "live"):
            for peer in read_live_peers(self.port):
                candidates.append((peer, "live"))
        if self.source in ("auto", "config"):
            config_hosts, config_path = read_remote_hosts(self.config_path)
            source = str(config_path) if config_path else "config"
            for host in config_hosts:
                candidates.append((host, source))
        if self.fallback_host and all(h != str(self.fallback_host) for h, _ in candidates):
            candidates.append((str(self.fallback_host), "fallback"))

        signature = "|".join(
            [self.template, self.source, *(f"{host}@{source}" for host, source in candidates)]
        )
        return candidates, signature

    def current_base_url(self, *, force: bool = False) -> str:
        if not self.dynamic:
            return self.template
        if not self.enabled:
            raise DeskflowResolveError(
                "base_url 含 {deskflow_host} 但未启用 deskflow.enabled；请启用或改成静态地址"
            )
        candidates, signature = self._collect_candidates()
        if (
            not force
            and self._cached
            and self._signature == signature
            and time.monotonic() - self._cached_at < self.cache_ttl
        ):
            return self._cached.base_url

        if not candidates:
            raise DeskflowResolveError(
                f"未找到 Deskflow 对端 IP（source={self.source}, port={self.port}）。"
                "请确认 Windows 端 Deskflow 已连接，或配置 llm.deskflow.fallback_host。"
            )

        failures: list[str] = []
        for candidate, source in candidates:
            host = _normalize_host(candidate)
            base_url = self.template.replace(HOST_PLACEHOLDER, host)
            health = health_url(base_url, self.health_path)
            if not self.probe or probe_health(health, self.probe_timeout):
                self._cached = ResolvedEndpoint(base_url=base_url, host=host, source=source)
                self._cached_at = time.monotonic()
                self._signature = signature
                return base_url
            failures.append(f"{source}:{candidate} → {health} 不通")

        if not force and self._cached:
            log.warning("Deskflow 候选地址探测均失败，沿用上次可用地址 %s", self._cached.base_url)
            return self._cached.base_url
        raise DeskflowResolveError("Deskflow 候选地址均不可用: " + "; ".join(failures))
