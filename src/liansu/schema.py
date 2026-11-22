"""连溯核心数据模型 — Fact schema (TECH_DESIGN §3)。

三元组骨架 + 原始 statement 证据 + 时效元数据。
真源为 append-only journal，Fact 的 YAML 表示是 journal 行的载荷。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Fact(BaseModel):
    id: str
    subject: str
    predicate: str
    object: str
    statement: str
    valid_from: date
    valid_to: date | None = None          # null = 当前仍成立
    confidence: float = Field(ge=0.0, le=1.0)
    topics: list[str] = Field(default_factory=list)
    source: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    supersedes: str | None = None          # 指向被覆盖的旧 fact id

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("fact id 不能为空")
        return v

    @field_validator("confidence")
    @classmethod
    def _check_conf(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence 必须在 [0,1]")
        return v

    @model_validator(mode="after")
    def _check_window(self) -> Fact:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to 不能早于 valid_from")
        return self

    def is_valid_at(self, when: date | None = None) -> bool:
        when = when or date.today()
        return self.valid_from <= when and (self.valid_to is None or self.valid_to >= when)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_yaml(self) -> str:
        """稳定序列化：字段按声明顺序，日期转 ISO 字符串。"""
        return yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False).strip()

    @classmethod
    def from_yaml(cls, text: str) -> Fact:
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("Fact YAML 必须是映射")
        return cls.model_validate(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fact:
        return cls.model_validate(data)

    def statement_for_index(self) -> str:
        """供索引使用的文本：statement 为主，拼接 subject/predicate/object/topics。"""
        return " ".join(
            [self.statement, self.subject, self.predicate, self.object, *self.topics]
        )
