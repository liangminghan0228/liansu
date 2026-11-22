"""本地 Embedder 实现：sentence-transformers 加载 bge-small-zh（延迟加载，避免 MCP 启动阻塞）。"""
from __future__ import annotations

from liansu.providers.base import Embedder


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5", device: str | None = None):
        self.model_name = model_name
        self.device = device
        self._model = None
        self._dim: int | None = None

    def _ensure(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name, device=self.device)
            self._dim = int(self._model.get_sentence_embedding_dimension())
        return self._model

    def embed(self, text: str) -> list[float]:
        model = self._ensure()
        vec = model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]

    def dimension(self) -> int:
        self._ensure()
        return int(self._dim or 0)
