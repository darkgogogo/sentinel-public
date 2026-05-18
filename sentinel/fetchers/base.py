"""Fetcher 抽象接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class FetchResult:
    url: str
    status_code: int
    body: str           # HTML / JSON / markdown 文本
    content_type: str = ""
    fetcher_name: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and not self.error and bool(self.body)


class FetchFailed(Exception):
    """所有 fetcher 都失败时抛。"""


class BaseFetcher(ABC):
    """Fetcher 抽象 · async fetch 接口。"""

    name: str = ""

    @abstractmethod
    async def fetch(self, url: str, *, timeout: float = 30.0,
                    headers: dict[str, str] | None = None) -> FetchResult:
        """抓 URL · 失败返回 FetchResult.ok=False（不抛）。"""
        ...


async def fetch_with_chain(url: str, fetchers: list[BaseFetcher], *,
                          timeout: float = 30.0,
                          headers: dict[str, str] | None = None) -> FetchResult:
    """按顺序尝试每个 fetcher，第一个 ok 即返回。

    全部失败抛 FetchFailed，含每个 fetcher 的错误。
    """
    errors = []
    for f in fetchers:
        try:
            r = await f.fetch(url, timeout=timeout, headers=headers)
            if r.ok:
                return r
            errors.append(f"{f.name}: status={r.status_code} {r.error}")
        except Exception as e:
            errors.append(f"{f.name}: {type(e).__name__}: {e}")
    raise FetchFailed(f"所有 fetcher 失败: {' | '.join(errors)}")
