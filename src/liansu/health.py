"""T17 · 服务可用性自检 —— 「连溯现在能不能用」的结构化答案。

给两类调用方：
- CLI：``liansu health --json``，退出码 0 表示可用（含降级），1 表示不可用；
- DSH 记忆插件：``/liansu/health`` 与 ``/liansu/metrics`` 的 ``service`` 字段据此渲染状态条。

契约（字段名是给插件的硬契约，不随实现变化）：

``{available, degraded, reason, checkedAt, version, python, root, checks[], capabilities}``

- ``available``：**必需项**全部通过。任何必需项失败即视为不可用（插件应整体隐藏/报错）；
- ``degraded``：可用但**可选项**失败（例如 LLM 不可达、向量模型未缓存），功能会打折；
- ``checks[]``：``{id, label, ok, level, detail, ms}``，``level`` 为 ``required`` 或 ``optional``；
- ``capabilities``：按能力给出的可用性（``extract`` / ``recall`` / ``inject``），
  每项 ``{ok, reason, requires[]}``。**LLM 不可用只影响 ``extract``**，已有记忆仍可检索、仍可注入。

只读、无副作用、尽量快：一次配置加载 + 若干 fs stat + 一次（可跳过的）LLM 健康探测。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from liansu import telemetry
from liansu.config import get_llm_config, load_config

__all__ = ["check_service", "REQUIRED", "OPTIONAL"]

REQUIRED = "required"
OPTIONAL = "optional"

#: 探测默认超时（秒）。内网 LLM 掉线时不应把面板拖住。
DEFAULT_TIMEOUT = 1.5

#: 向量模型默认名，与 config.yaml 的 embedder.model 对齐。
DEFAULT_EMBEDDER = "BAAI/bge-small-zh-v1.5"


def _check(
    checks: list[dict[str, Any]],
    ident: str,
    label: str,
    ok: bool,
    detail: str,
    *,
    level: str = REQUIRED,
    elapsed_ms: float = 0.0,
) -> None:
    checks.append({
        "id": ident,
        "label": label,
        "ok": bool(ok),
        "level": level,
        "detail": detail,
        "ms": round(float(elapsed_ms), 1),
    })


def _http_ok(url: str, timeout: float, api_key: str = "") -> bool:
    """GET 一个 JSON 端点并只要 2xx；显式禁用系统代理（内网端点常被 HTTP_PROXY 拦）。"""
    try:
        from urllib.request import ProxyHandler, Request, build_opener

        headers = {"accept": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request(url, headers=headers), timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except Exception:
        return False


def _llm_check(cfg: dict[str, Any], timeout: float, probe: bool) -> tuple[bool, str]:
    """探测 LLM 端点：返回 (ok, detail)。异常一律折成「不可用 + 原因」，不抛。"""
    try:
        from liansu.deskflow import DeskflowEndpointResolver, DeskflowResolveError, health_url, probe_health
    except Exception as exc:  # pragma: no cover - 模块缺失属于安装损坏
        return False, f"deskflow 模块不可用: {exc}"

    llm = cfg.get("llm") or {}
    model = str(llm.get("model") or "")
    started = time.perf_counter()
    try:
        resolver = DeskflowEndpointResolver(llm)
        base_url = resolver.current_base_url()
    except DeskflowResolveError as exc:
        return False, f"解析 LLM 端点失败: {exc}"
    except Exception as exc:  # pragma: no cover - 防御
        return False, f"解析 LLM 端点异常: {exc}"

    if not base_url:
        return False, "未配置 llm.base_url"
    if not probe:
        return True, f"{base_url}（跳过探测）"

    endpoint = str(base_url).rstrip("/")
    # 优先探 OpenAI 兼容面（/models）：它同时验证「进程活着」和「API 路由正确」；
    # 非 /v1 形态（自建网关）再退回 /health。
    if endpoint.endswith("/v1"):
        url = f"{endpoint}/models"
        api_key = str(llm.get("api_key") or "")
        ok = _http_ok(url, timeout, api_key)
        kind = "models"
    else:
        url = health_url(endpoint, getattr(resolver, "health_path", "/health"))
        ok = probe_health(url, timeout)
        kind = "health"
    elapsed = (time.perf_counter() - started) * 1000
    if ok:
        return True, f"{base_url} · {model or '默认模型'} · {kind} {elapsed:.0f}ms"
    return False, f"LLM 不可达：{url}（{model or '默认模型'}，{timeout}s 超时）"


def _embedder_cached(model: str) -> bool | None:
    """查 HuggingFace 本地缓存，判断向量模型是否已下载；查不到返回 None（未知）。"""
    home = Path.home() / ".cache" / "huggingface" / "hub"
    folder = "models--" + model.replace("/", "--")
    try:
        return (home / folder).exists()
    except OSError:  # pragma: no cover - 权限异常
        return None


def check_service(
    root: Path | str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    probe_llm: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """跑一遍自检并返回契约对象；永不抛异常（内部异常折成失败项）。"""
    current = now or datetime.now().astimezone()
    vault_root = telemetry.resolve_root(root)
    checks: list[dict[str, Any]] = []

    # 1) 真源目录：必需。
    started = time.perf_counter()
    root_ok = vault_root.is_dir()
    _check(checks, "root", "vault 根目录", root_ok,
           str(vault_root) if root_ok else f"目录不存在: {vault_root}",
           elapsed_ms=(time.perf_counter() - started) * 1000)

    # 2) 配置：必需（CLI 的其它命令都依赖它）。
    cfg: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        cfg = load_config()
        _check(checks, "config", "config.yaml", True, "已加载", elapsed_ms=(time.perf_counter() - started) * 1000)
    except Exception as exc:
        _check(checks, "config", "config.yaml", False, f"加载失败: {exc}",
               elapsed_ms=(time.perf_counter() - started) * 1000)

    # 3) journal：必需（记忆真源）。
    journal_dir = vault_root / "journal"
    journal_ok = journal_dir.is_dir()
    _check(checks, "journal", "journal 真源", journal_ok,
           "存在" if journal_ok else f"缺失: {journal_dir}")

    # 4) 索引库：可选（缺失只是冷启动，可 rebuild）。
    index_db = vault_root / ".liansu" / "index.db"
    try:
        size = index_db.stat().st_size
        _check(checks, "index", "索引 index.db", True, f"{size / 1024:.0f} KB", level=OPTIONAL)
    except OSError:
        _check(checks, "index", "索引 index.db", False, "缺失，首次检索会变慢（可 rebuild）", level=OPTIONAL)

    # 5) 视图缓存：可选。
    brief = vault_root / "vault_views" / "brief.md"
    _check(checks, "views", "视图缓存", brief.exists(),
           "brief.md 存在" if brief.exists() else "brief.md 缺失（下次抽取后生成）", level=OPTIONAL)

    # 6) 库体量：只读统计，失败不影响可用性。
    started = time.perf_counter()
    try:
        from liansu.metrics import vault_stats
        stats = vault_stats(vault_root)
        detail = f"事实 {stats.get('facts', 0)} · 失效 {stats.get('stale', 0)} · 实体 {stats.get('entities', 0)} · 主题 {stats.get('topics', 0)}"
        _check(checks, "vault", "库体量", True, detail, level=OPTIONAL,
               elapsed_ms=(time.perf_counter() - started) * 1000)
    except Exception as exc:
        _check(checks, "vault", "库体量", False, f"统计失败: {exc}", level=OPTIONAL,
               elapsed_ms=(time.perf_counter() - started) * 1000)

    # 7) LLM：可选但影响抽取/摘要质量。
    llm_ok, llm_detail = _llm_check(cfg, timeout=timeout, probe=probe_llm)
    _check(checks, "llm", "LLM 端点", llm_ok, llm_detail, level=OPTIONAL)

    # 8) 向量模型：可选。
    embedder = str((cfg.get("embedder") or {}).get("model") or DEFAULT_EMBEDDER)
    cached = _embedder_cached(embedder)
    if cached is None:
        _check(checks, "embedder", "向量模型", True, f"{embedder}（缓存状态未知）", level=OPTIONAL)
    else:
        _check(checks, "embedder", "向量模型", cached,
               f"{embedder} · " + ("已缓存" if cached else "未下载，首次向量检索会拉模型"),
               level=OPTIONAL)

    failed_required = [item for item in checks if not item["ok"] and item["level"] == REQUIRED]
    failed_optional = [item for item in checks if not item["ok"] and item["level"] == OPTIONAL]
    available = not failed_required
    degraded = available and bool(failed_optional)

    if not available:
        reason = "；".join(f"{item['label']}：{item['detail']}" for item in failed_required)
    elif degraded:
        reason = "；".join(f"{item['label']}：{item['detail']}" for item in failed_optional)
    else:
        reason = ""

    # 能力视图：LLM 只决定「能不能抽取」，检索与注入不依赖它。
    index_ok = next((item["ok"] for item in checks if item["id"] == "index"), False)
    views_ok = next((item["ok"] for item in checks if item["id"] == "views"), False)
    embedder_ok = next((item["ok"] for item in checks if item["id"] == "embedder"), False)
    capabilities = {
        "extract": {
            "ok": available and llm_ok,
            "reason": "" if (available and llm_ok) else (llm_detail if available else reason),
            "requires": ["llm", "journal"],
        },
        "recall": {
            "ok": available,
            "reason": "" if available else reason,
            "requires": ["journal"],
            "note": "" if embedder_ok else "向量模型未缓存，仅 BM25/图检索可用",
        },
        "inject": {
            "ok": available,
            "reason": "" if available else reason,
            "requires": ["journal"],
            "note": "" if views_ok else "brief 未生成，注入为空",
        },
    }

    try:
        from liansu import __version__ as version
    except Exception:  # pragma: no cover - 版本号缺失不影响判断
        version = ""

    return {
        "available": available,
        "degraded": degraded,
        "reason": reason,
        "checkedAt": current.isoformat(timespec="seconds"),
        "version": version,
        "python": sys.version.split()[0],
        "root": str(vault_root),
        "checks": checks,
        "capabilities": capabilities,
    }
