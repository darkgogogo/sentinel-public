"""Twitter collector · SocialData API。

需要 SOCIALDATA_API_KEY（社区 API，按 user-handle 抓 timeline）。
identifier: 不带 @ 的 username（如 'vpnnews_cn'）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from sentinel.collectors import register
from sentinel.collectors.base import BaseCollector, CollectedMessage


log = logging.getLogger(__name__)


SOCIALDATA_BASE = "https://api.socialdata.tools"


@register
class TwitterCollector(BaseCollector):
    KIND = "twitter"

    async def collect(self, since: datetime) -> AsyncIterator[CollectedMessage]:
        secrets = self.config.get("secrets", {})
        api_key = secrets.get("SOCIALDATA_API_KEY", "")
        if not api_key:
            raise RuntimeError("缺 SOCIALDATA_API_KEY")

        handle = self.source["identifier"].lstrip("@")
        # SocialData /twitter/user/{handle}/tweets
        url = f"{SOCIALDATA_BASE}/twitter/user/{handle}/tweets"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"SocialData {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        tweets = data.get("tweets", data) if isinstance(data, dict) else data
        if not isinstance(tweets, list):
            raise RuntimeError(f"unexpected payload shape: {type(tweets)}")

        for t in tweets:
            tid = str(t.get("id_str") or t.get("id") or "")
            text = t.get("text") or t.get("full_text") or ""
            if not tid or not text:
                continue
            created_at = t.get("created_at") or t.get("tweet_created_at")
            posted_at: datetime
            try:
                # SocialData 用 Twitter 原始格式：'Wed Oct 10 20:19:24 +0000 2018'
                posted_at = datetime.strptime(
                    created_at, "%a %b %d %H:%M:%S %z %Y")
            except (TypeError, ValueError):
                posted_at = datetime.now(timezone.utc)
            if posted_at < since:
                continue
            user = t.get("user", {}) or {}
            author = user.get("screen_name") or handle
            yield CollectedMessage(
                external_id=tid,
                author=author,
                content=text,
                url=f"https://twitter.com/{author}/status/{tid}",
                posted_at=posted_at,
                raw_json={"tweet_id": tid},
            )

    @classmethod
    async def resolve(cls, identifier: str, config: dict) -> dict:
        secrets = config.get("secrets", {})
        api_key = secrets.get("SOCIALDATA_API_KEY", "")
        if not api_key:
            raise ValueError("缺 SOCIALDATA_API_KEY")
        handle = identifier.lstrip("@").strip()
        if not handle:
            raise ValueError("identifier 不能为空")
        url = f"{SOCIALDATA_BASE}/twitter/user/{handle}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code != 200:
            raise ValueError(
                f"twitter user 不存在或 API 错误: {resp.status_code}")
        data = resp.json()
        return {
            "display_name": data.get("name") or f"@{handle}",
        }
