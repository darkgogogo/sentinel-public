"""CdpFetcher · playwright + 本地 Chrome CDP（**stub · 按需实现**）。

设计目的：复用用户本地 Chrome 已登录的 session，抓需要登录态的内容。

实现要点（实际开发时）：
1. 用户先启 Chrome 调试模式：`/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222`
2. playwright `connect_over_cdp("ws://localhost:9222")`
3. 找到当前已登录的页面 context，create_page 抓取
4. wait_for_load_state + extract body html / inner_text

当前 stub 直接返回 not_implemented。按需打开真实实现。
"""
from __future__ import annotations

from sentinel.fetchers.base import BaseFetcher, FetchResult


class CdpFetcher(BaseFetcher):
    """playwright + 本地 Chrome CDP · stub。"""

    name = "cdp"

    def __init__(self, cdp_endpoint: str = "ws://localhost:9222"):
        self.cdp_endpoint = cdp_endpoint

    async def fetch(self, url: str, *, timeout: float = 30.0,
                    headers: dict[str, str] | None = None) -> FetchResult:
        return FetchResult(
            url=url, status_code=0, body="",
            fetcher_name=self.name,
            error="not_implemented · 按需实现，参见模块 docstring",
        )
