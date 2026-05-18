"""Fetcher 工具层 + F2 新 collectors（twitter/reddit/social）测试。"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sentinel.collectors import KIND_REGISTRY, auto_discover
from sentinel.fetchers import (
    BaseFetcher, FetchResult, FetchFailed, fetch_with_chain,
    HttpxFetcher, JinaFetcher, CdpFetcher,
)


# ============== Fetcher 抽象 ==============


def test_fetch_result_ok_property():
    r = FetchResult(url="x", status_code=200, body="hi")
    assert r.ok is True
    r2 = FetchResult(url="x", status_code=200, body="")
    assert r2.ok is False  # 空 body 不算成功
    r3 = FetchResult(url="x", status_code=404, body="oops")
    assert r3.ok is False


def test_fetcher_chain_picks_first_ok():
    class GoodFetcher(BaseFetcher):
        name = "good"
        async def fetch(self, url, *, timeout=30.0, headers=None):
            return FetchResult(url, 200, "yes", fetcher_name="good")

    class BadFetcher(BaseFetcher):
        name = "bad"
        async def fetch(self, url, *, timeout=30.0, headers=None):
            return FetchResult(url, 500, "", fetcher_name="bad",
                              error="oops")

    import asyncio
    r = asyncio.run(fetch_with_chain("https://x", [BadFetcher(), GoodFetcher()]))
    assert r.ok and r.fetcher_name == "good"


def test_fetcher_chain_raises_when_all_fail():
    class BadFetcher(BaseFetcher):
        name = "bad"
        async def fetch(self, url, *, timeout=30.0, headers=None):
            return FetchResult(url, 500, "", fetcher_name="bad", error="x")

    import asyncio
    with pytest.raises(FetchFailed):
        asyncio.run(fetch_with_chain("https://x", [BadFetcher(), BadFetcher()]))


def test_cdp_fetcher_stub_returns_not_implemented():
    import asyncio
    r = asyncio.run(CdpFetcher().fetch("https://x"))
    assert not r.ok
    assert "not_implemented" in r.error


# ============== auto_discover 含 F2 ==============


def test_auto_discover_registers_all_f2_collectors():
    auto_discover()
    assert "telegram" in KIND_REGISTRY
    assert "rss" in KIND_REGISTRY
    assert "twitter" in KIND_REGISTRY
    assert "reddit" in KIND_REGISTRY
    assert "social" in KIND_REGISTRY


# ============== Twitter ==============


async def test_twitter_requires_socialdata_key():
    from sentinel.collectors.twitter import TwitterCollector
    coll = TwitterCollector(
        source_row={"id": 1, "kind": "twitter", "identifier": "elonmusk"},
        config={"secrets": {}},
    )
    with pytest.raises(RuntimeError, match="SOCIALDATA_API_KEY"):
        async for _ in coll.collect(datetime.now(timezone.utc)):
            pass


async def test_twitter_resolve_requires_socialdata_key():
    from sentinel.collectors.twitter import TwitterCollector
    with pytest.raises(ValueError, match="SOCIALDATA_API_KEY"):
        await TwitterCollector.resolve("elonmusk", {"secrets": {}})


# ============== Reddit ==============


async def test_reddit_uses_rss_endpoint(monkeypatch):
    """Reddit collect 应转化为 RSS URL 调 RSSCollector。"""
    from sentinel.collectors.reddit import RedditCollector

    captured = {}

    async def fake_rss_collect(self, since):
        captured["identifier"] = self.source["identifier"]
        return
        yield  # unreachable

    monkeypatch.setattr(
        "sentinel.collectors.rss.RSSCollector.collect", fake_rss_collect)
    coll = RedditCollector(
        source_row={"id": 1, "kind": "reddit", "identifier": "dumbclub"},
        config={},
    )
    async for _ in coll.collect(datetime.now(timezone.utc)):
        pass
    assert captured["identifier"] == "https://www.reddit.com/r/dumbclub/.rss"


async def test_reddit_resolve_strips_prefix():
    from sentinel.collectors.reddit import RedditCollector
    # 实际会调 feedparser 联网，monkeypatch RSSCollector.resolve
    with patch("sentinel.collectors.rss.RSSCollector.resolve",
               new=AsyncMock(return_value={"display_name": "x"})):
        meta = await RedditCollector.resolve("r/dumbclub", {})
        assert meta["display_name"] == "r/dumbclub"
        meta2 = await RedditCollector.resolve("dumbclub", {})
        assert meta2["display_name"] == "r/dumbclub"


# ============== Social ==============


def test_social_parse_identifier():
    from sentinel.collectors.social import _parse_identifier
    assert _parse_identifier("v2ex") == ("v2ex", "")
    assert _parse_identifier("v2ex/vpn") == ("v2ex", "vpn")
    assert _parse_identifier("zhihu/翻墙 GFW") == ("zhihu", "翻墙 GFW")


async def test_social_resolve_supported_platforms():
    from sentinel.collectors.social import SocialCollector, SUPPORTED_PLATFORMS
    for p in SUPPORTED_PLATFORMS:
        meta = await SocialCollector.resolve(p, {})
        assert "display_name" in meta


async def test_social_resolve_rejects_unknown():
    from sentinel.collectors.social import SocialCollector
    with pytest.raises(ValueError, match="unsupported"):
        await SocialCollector.resolve("不存在的平台", {})


async def test_social_unimplemented_platform_raises():
    """xiaohongshu 等未实现的平台应 raise NotImplementedError 或 RuntimeError。"""
    from sentinel.collectors.social import SocialCollector
    coll = SocialCollector(
        source_row={"id": 1, "kind": "social", "identifier": "xiaohongshu"},
        config={},
    )
    with pytest.raises((NotImplementedError, RuntimeError)):
        async for _ in coll.collect(datetime.now(timezone.utc)):
            pass


async def test_social_v2ex_mock_fetch(monkeypatch):
    """mock HttpxFetcher 返回 v2ex RSS 样本，验证解析。"""
    fake_rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>V2EX</title>
<item>
  <title>测试帖子</title>
  <link>https://v2ex.com/t/12345</link>
  <description>这是一段帖子内容</description>
  <pubDate>Thu, 15 May 2026 10:00:00 GMT</pubDate>
  <author>testuser</author>
  <guid>https://v2ex.com/t/12345</guid>
</item>
</channel></rss>"""

    async def fake_fetch(self, url, *, timeout=30.0, headers=None):
        return FetchResult(url=url, status_code=200, body=fake_rss,
                          fetcher_name="httpx")
    monkeypatch.setattr(
        "sentinel.fetchers.httpx_fetcher.HttpxFetcher.fetch", fake_fetch)

    from sentinel.collectors.social import SocialCollector
    coll = SocialCollector(
        source_row={"id": 1, "kind": "social", "identifier": "v2ex"},
        config={},
    )
    msgs = []
    async for m in coll.collect(datetime(2026, 1, 1, tzinfo=timezone.utc)):
        msgs.append(m)
    assert len(msgs) == 1
    assert "测试帖子" in msgs[0].content
    assert "12345" in msgs[0].url
