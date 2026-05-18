"""Fetchers 工具层（借鉴 web-access site-pattern 沉淀）。

- BaseFetcher 接口统一，可降级链调用
- HttpxFetcher (默认) 直连
- JinaFetcher (r.jina.ai) 反爬转 markdown
- CdpFetcher (stub) · playwright + Chrome CDP，按需实现
"""
from sentinel.fetchers.base import (
    BaseFetcher, FetchResult, FetchFailed, fetch_with_chain,
)
from sentinel.fetchers.httpx_fetcher import HttpxFetcher
from sentinel.fetchers.jina_fetcher import JinaFetcher
from sentinel.fetchers.cdp_fetcher import CdpFetcher

__all__ = [
    "BaseFetcher",
    "FetchResult",
    "FetchFailed",
    "fetch_with_chain",
    "HttpxFetcher",
    "JinaFetcher",
    "CdpFetcher",
]
