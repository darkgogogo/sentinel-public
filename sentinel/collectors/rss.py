"""RSS collector · feedparser + HTML 剥离 + title 合并到 content。"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import AsyncIterator
from urllib.parse import urlparse

from sentinel.collectors import register
from sentinel.collectors.base import BaseCollector, CollectedMessage


_HTML_BLOCK = re.compile(
    r"<(script|style)[^>]*>.*?</\1>|<!--.*?-->", re.DOTALL | re.IGNORECASE
)
_HTML_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    s = _HTML_BLOCK.sub("", s)
    s = _HTML_TAGS.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()
    return s


def _entry_content(entry: dict) -> str:
    """合并 title + summary。reddit RSS 的 summary 几乎只有 'submitted by /u/xxx'，
    把 title 拼进 content 才能让英文圈品牌的 keyword 命中。
    """
    title = (entry.get("title") or "").strip()
    summary = (entry.get("summary") or "").strip()
    summary = _strip_html(summary)
    if title and summary:
        return f"{title}\n\n{summary}"
    return title or summary


def _entry_posted_at(entry: dict) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


@register
class RSSCollector(BaseCollector):
    KIND = "rss"

    async def collect(self, since: datetime) -> AsyncIterator[CollectedMessage]:
        import feedparser

        url = self.source["identifier"]
        # feedparser 是同步阻塞 IO，丢线程池
        feed = await asyncio.to_thread(feedparser.parse, url)

        if feed.get("bozo") and feed.get("entries", []) == []:
            raise RuntimeError(
                f"RSS parse 失败: {url} · {feed.get('bozo_exception')}")

        for entry in feed.get("entries", []):
            posted_at = _entry_posted_at(entry)
            if posted_at < since:
                continue
            content = _entry_content(entry)
            if not content:
                continue
            external_id = entry.get("id") or entry.get("link") or content[:80]
            yield CollectedMessage(
                external_id=str(external_id),
                author=entry.get("author"),
                content=content,
                url=entry.get("link"),
                posted_at=posted_at,
                raw_json={"feed_title": feed.feed.get("title", "")},
            )

    @classmethod
    async def resolve(cls, identifier: str, config: dict) -> dict:
        import feedparser
        # 简单校验：是 URL + 能解析
        parsed = urlparse(identifier)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"RSS identifier 必须是 http(s) URL: {identifier!r}")

        feed = await asyncio.to_thread(feedparser.parse, identifier)
        if feed.get("bozo") and not feed.get("entries"):
            raise ValueError(
                f"RSS 解析失败: {feed.get('bozo_exception')}")
        title = feed.feed.get("title", "") or identifier
        return {"display_name": title}
