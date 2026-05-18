"""BaseCollector 接口 · 所有 collector kind 走统一接口。

新增 collector kind = 写一个 BaseCollector 子类 + @register 即可。
schema 不动、collect service 不动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator


@dataclass
class CollectedMessage:
    external_id: str
    author: str | None
    content: str
    url: str | None
    posted_at: datetime
    raw_json: dict


class BaseCollector(ABC):
    KIND: str = ""

    def __init__(self, source_row: dict, config: dict):
        self.source = source_row
        self.config = config

    @abstractmethod
    async def collect(self, since: datetime) -> AsyncIterator[CollectedMessage]:
        """从该 source 拉 since 之后的消息。

        子类应 yield CollectedMessage。单源失败由 collect service 隔离，
        本方法**不需要**做 try/except 兜底。
        """
        ...

    @classmethod
    @abstractmethod
    async def resolve(cls, identifier: str, config: dict) -> dict:
        """add-source 时验证 identifier + 返回元数据（display_name 等）。

        失败抛 ValueError。
        """
        ...
