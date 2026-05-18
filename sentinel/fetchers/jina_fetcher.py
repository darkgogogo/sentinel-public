"""JinaFetcher · r.jina.ai 把 HTML 转 markdown 省 token。

适用：反爬站点 + 内容主体提取（去掉广告/导航等噪音）。
免费 quota 有限，按需用。
"""
from __future__ import annotations

import httpx

from sentinel.fetchers.base import BaseFetcher, FetchResult


class JinaFetcher(BaseFetcher):
    """r.jina.ai/{url} · 返回纯净 markdown · 无 API key 也能用，但有限速。"""

    name = "jina"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key  # 可选；不填走免费层

    async def fetch(self, url: str, *, timeout: float = 30.0,
                    headers: dict[str, str] | None = None) -> FetchResult:
        target = f"https://r.jina.ai/{url}"
        h = {"Accept": "text/plain"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if headers:
            h.update(headers)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(target, headers=h)
            return FetchResult(
                url=url,
                status_code=resp.status_code,
                body=resp.text,
                content_type="text/markdown",
                fetcher_name=self.name,
            )
        except Exception as e:
            return FetchResult(
                url=url, status_code=0, body="",
                fetcher_name=self.name,
                error=f"{type(e).__name__}: {e}",
            )
