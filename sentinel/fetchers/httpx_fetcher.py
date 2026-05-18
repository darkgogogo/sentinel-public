"""HttpxFetcher · 直连 HTTP，最简单最快。"""
from __future__ import annotations

import httpx

from sentinel.fetchers.base import BaseFetcher, FetchResult


DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class HttpxFetcher(BaseFetcher):
    """httpx 直连 · default UA + cookie 支持。"""

    name = "httpx"

    def __init__(self, default_ua: str = DEFAULT_UA):
        self.default_ua = default_ua

    async def fetch(self, url: str, *, timeout: float = 30.0,
                    headers: dict[str, str] | None = None) -> FetchResult:
        h = {"User-Agent": self.default_ua}
        if headers:
            h.update(headers)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True,
            ) as client:
                resp = await client.get(url, headers=h)
            return FetchResult(
                url=str(resp.url),
                status_code=resp.status_code,
                body=resp.text,
                content_type=resp.headers.get("content-type", ""),
                fetcher_name=self.name,
            )
        except Exception as e:
            return FetchResult(
                url=url, status_code=0, body="",
                fetcher_name=self.name,
                error=f"{type(e).__name__}: {e}",
            )
