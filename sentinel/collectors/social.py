"""Social platform collector · per-platform dispatch · 用 Fetchers 工具层。

支持的平台（按抓取难度）：
- v2ex     ⭐ 简单（公开 HTML，httpx 直接抓）  · 已实现
- bilibili ⭐ 友好 API （httpx）                · stub TODO
- jike     ⭐⭐ 需要 token  (httpx + jina)       · stub TODO
- zhihu    ⭐⭐⭐ 反爬严，需 jina + cdp           · stub TODO
- xiaohongshu ⭐⭐⭐⭐ 最严，签名+IP+设备指纹     · stub TODO

identifier: 'platform/query'（如 'v2ex/vpn' 表示 v2ex 关键词 vpn）
或 platform 单独表示 hot 流。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import AsyncIterator

from sentinel.collectors import register
from sentinel.collectors.base import BaseCollector, CollectedMessage
from sentinel.fetchers import HttpxFetcher, FetchFailed


log = logging.getLogger(__name__)


SUPPORTED_PLATFORMS = {
    "v2ex": "V2EX",
    "bilibili": "B站",
    "jike": "即刻",
    "zhihu": "知乎",
    "xiaohongshu": "小红书",
}


def _parse_identifier(identifier: str) -> tuple[str, str]:
    """'platform/query' → (platform, query)。无 / → query=''。"""
    parts = identifier.split("/", 1)
    if len(parts) == 1:
        return parts[0].lower(), ""
    return parts[0].lower(), parts[1].strip()


@register
class SocialCollector(BaseCollector):
    KIND = "social"

    async def collect(self, since: datetime) -> AsyncIterator[CollectedMessage]:
        platform, query = _parse_identifier(self.source["identifier"])
        handler = _PLATFORM_HANDLERS.get(platform)
        if not handler:
            raise RuntimeError(
                f"unknown social platform: {platform!r} · "
                f"支持: {list(SUPPORTED_PLATFORMS)}")
        async for msg in handler(self, since, query):
            yield msg

    @classmethod
    async def resolve(cls, identifier: str, config: dict) -> dict:
        platform, query = _parse_identifier(identifier)
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError(
                f"unsupported social platform: {platform!r} · "
                f"支持: {list(SUPPORTED_PLATFORMS)}")
        display = SUPPORTED_PLATFORMS[platform]
        if query:
            display = f"{display}·{query}"
        return {"display_name": display}


# ===================== per-platform 实现 =====================


async def _collect_v2ex(coll: SocialCollector, since: datetime,
                       query: str) -> AsyncIterator[CollectedMessage]:
    """V2EX · 用首页 RSS（公开 + httpx 友好）。

    query 不空 → 走站内搜索（暂不实现，TODO）；默认抓首页热门主题。
    """
    fetcher = HttpxFetcher()
    if query:
        log.warning("v2ex search query 暂不实现，回退到首页热门")

    # V2EX 首页 RSS（公开）
    url = "https://www.v2ex.com/index.xml"
    r = await fetcher.fetch(url)
    if not r.ok:
        raise FetchFailed(f"v2ex 抓取失败: status={r.status_code}")

    # 用 feedparser 解析 RSS（同 rss collector 复用思路）
    import feedparser
    feed = feedparser.parse(r.body)
    for entry in feed.get("entries", []):
        posted_at: datetime
        for key in ("published_parsed", "updated_parsed"):
            t = entry.get(key)
            if t:
                posted_at = datetime(*t[:6], tzinfo=timezone.utc)
                break
        else:
            posted_at = datetime.now(timezone.utc)
        if posted_at < since:
            continue
        title = (entry.get("title") or "").strip()
        summary = re.sub(r"<[^>]+>", " ", entry.get("summary") or "").strip()
        content = f"{title}\n\n{summary}".strip()
        if not content:
            continue
        external_id = entry.get("id") or entry.get("link") or content[:80]
        yield CollectedMessage(
            external_id=str(external_id),
            author=entry.get("author"),
            content=content,
            url=entry.get("link"),
            posted_at=posted_at,
            raw_json={"platform": "v2ex"},
        )


async def _collect_stub(platform_label: str):
    """其他平台的 stub generator（抛 NotImplementedError）。"""
    async def _impl(coll, since, query):
        raise NotImplementedError(
            f"{platform_label} 抓取未实现（需 fetcher 工具层扩展 + site-pattern 沉淀）"
        )
        yield  # unreachable, keep generator type
    return _impl


_PLATFORM_HANDLERS = {
    "v2ex": _collect_v2ex,
    # 以下按需实现（Phase F.2 后续 / 用户需要时）
    "bilibili": None,
    "jike": None,
    "zhihu": None,
    "xiaohongshu": None,
}
