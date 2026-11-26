"""配置加载 — base_url/api_key/model 支持环境变量覆盖，IP 变更只改 config 一处。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# 环境变量名：优先级高于 config.yaml
_ENV_OVERRIDES = {
    ("llm", "base_url"): "LIANSU_LLM_BASE_URL",
    ("llm", "api_key"): "LIANSU_LLM_API_KEY",
    ("llm", "model"): "LIANSU_LLM_MODEL",
}

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config.yaml"
_EXAMPLE_CONFIG_PATH = _REPO_ROOT / "config.example.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """加载配置：显式路径 > config.yaml > config.example.yaml。

    本地真实配置不入库；新克隆可用示例配置直接跑通离线链路。
    """
    cfg_path = Path(path) if path else (_DEFAULT_CONFIG_PATH if _DEFAULT_CONFIG_PATH.exists() else _EXAMPLE_CONFIG_PATH)
    if not cfg_path.exists():
        raise FileNotFoundError(f"config 不存在: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    for (section, key), env_name in _ENV_OVERRIDES.items():
        if env_name in os.environ:
            cfg.setdefault(section, {})[key] = os.environ[env_name]
    return cfg


def get_llm_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else load_config()
    return cfg.get("llm", {})


def resolve_vault_root(root: str | Path | None = None, cfg: dict[str, Any] | None = None) -> Path:
    """解析 vault 根（真源所在），统一入口。

    优先级：显式参数 > ``LIANSU_ROOT`` 环境变量 > ``config.vault.root`` > 仓库根。
    前两者供测试/评测隔离用；生产默认由配置项指向 Obsidian vault。
    """
    if root is not None:
        return Path(root).expanduser().resolve()
    override = os.environ.get("LIANSU_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    cfg = cfg if cfg is not None else load_config()
    configured = (cfg.get("vault") or {}).get("root")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]
