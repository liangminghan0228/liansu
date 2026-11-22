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

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else _DEFAULT_CONFIG_PATH
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
