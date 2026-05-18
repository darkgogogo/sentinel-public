"""Reddit collector · 通过 RSS 端点 reddit.com/r/X/.rss。

v1.1 决策沿用：因 Anthropic Responsible Builder Policy 限制 OAuth，
改走 RSS 端点（reddit 公开提供）。

identifier: subreddit 名（不带 r/，如 'dumbclub'）。
"""
from __future__ import annotations

from datetime import datetime
from typing import AsyncIterator

from sentinel.collectors import register
from sentinel.collectors.base import BaseCollector, CollectedMessage
from sentinel.collectors.rss import RSSCollector


@register
class RedditCollector(BaseCollector):
    KIND = "reddit"

    async def collect(self, since: datetime) -> AsyncIterator[CollectedMessage]:
        subreddit = self.source["identifier"].lstrip("/").removeprefix("r/")
        rss_url = f"https://www.reddit.com/r/{subreddit}/.rss"
        # 复用 RSSCollector 实现 · 改成 RSS source 行
        rss_source = {**self.source, "identifier": rss_url}
        delegate = RSSCollector(source_row=rss_source, config=self.config)
        async for msg in delegate.collect(since):
            yield msg

    @classmethod
    async def resolve(cls, identifier: str, config: dict) -> dict:
        subreddit = identifier.lstrip("/").removeprefix("r/").strip()
        if not subreddit:
            raise ValueError("identifier 不能为空")
        rss_url = f"https://www.reddit.com/r/{subreddit}/.rss"
        meta = await RSSCollector.resolve(rss_url, config)
        return {
            "display_name": f"r/{subreddit}",
        }
