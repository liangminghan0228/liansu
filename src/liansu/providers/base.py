"""Provider 抽象层 — gate / extract / judge 与 embed 统一接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from liansu.schema import Fact


class LLMProvider(ABC):
    """LLM 能力：值得记判断、结构化抽取、LLM-as-Judge 打分。"""

    @abstractmethod
    def gate(self, dialog: list[dict[str, str]]) -> bool:
        """判断这段对话是否值得长期记忆。"""
        raise NotImplementedError

    @abstractmethod
    def extract(self, dialog: list[dict[str, str]], source: str = "") -> list[Fact]:
        """从对话中抽取结构化事实。"""
        raise NotImplementedError

    @abstractmethod
    def judge(self, question: str, facts: list[Fact], answer: str) -> dict:
        """对答案/事实做质量打分（供评测复用）。"""
        raise NotImplementedError


class Embedder(ABC):
    """文本向量化接口，向量索引只依赖此抽象。"""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    @abstractmethod
    def dimension(self) -> int:
        raise NotImplementedError
