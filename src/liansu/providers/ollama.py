"""可选离线兜底 provider — 本机 Ollama（localhost:11434）。

注意：Windows 主机 192.168.0.101:8080（2026-11-23 实测，此前为 .100）是 llama.cpp 的 OpenAI-compatible 服务，
**不是 Ollama 协议**——其 /api/chat 与 /api/generate 均返回 404，禁止把 Ollama
client 的 base_url 指到 8080，也禁止把 ApiProvider 指到 11434 之外混用两者。
本 provider 仅复用 Ollama 自带的 OpenAI-compatible /v1 端点，不引入 ollama 包依赖。
"""
from __future__ import annotations

from typing import Any

from liansu.providers.api import ApiProvider

__all__ = ["OllamaProvider"]


class OllamaProvider(ApiProvider):
    """本机 Ollama 离线兜底（继承 ApiProvider 的 gate/extract/judge 实现）。

    base_url 固定为本机 localhost:11434/v1，绝不读取 config 里的 Windows 端点；
    model 可由构造参数或 config 的 llm.ollama_model 指定，默认 qwen3。
    """

    OLLAMA_BASE_URL = "http://localhost:11434/v1"
    DEFAULT_MODEL = "qwen3"

    def __init__(self, model: str | None = None, config: dict[str, Any] | None = None, *, client: Any | None = None):
        llm = (config or {}).get("llm", {})
        chosen_model = model or llm.get("ollama_model") or self.DEFAULT_MODEL
        ollama_config = {
            "llm": {
                "base_url": self.OLLAMA_BASE_URL,  # 唯一允许的端点
                "api_key": "ollama",               # 本地服务不校验
                "model": chosen_model,
                "extra_body": {},                  # Ollama 不需要 Qwen3 关 thinking 参数
            }
        }
        super().__init__(ollama_config, client=client)
        self._extra_body = {}
