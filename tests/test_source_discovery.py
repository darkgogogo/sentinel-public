"""Source discovery 测试 · LLM 推荐信源."""
from __future__ import annotations

import pytest

from sentinel.config import Config
from sentinel.services.source_discovery import (
    _extract_json_list, _normalize_candidate, discover_sources,
)


def test_extract_json_list_plain():
    assert _extract_json_list('[{"a":1},{"b":2}]') == [{"a": 1}, {"b": 2}]


def test_extract_json_list_with_fence():
    text = '''```json
[
  {"kind": "rss", "identifier": "https://x.com/rss"}
]
```'''
    out = _extract_json_list(text)
    assert len(out) == 1
    assert out[0]["kind"] == "rss"


def test_extract_json_list_with_preamble():
    text = '这是给你的推荐：\n[{"kind":"rss","identifier":"https://x.com/rss"}]\n希望有用'
    out = _extract_json_list(text)
    assert len(out) == 1


def test_extract_json_list_invalid():
    assert _extract_json_list("") == []
    assert _extract_json_list("not json") == []
    assert _extract_json_list('{"single": "object"}') == []  # 不是 list


def test_normalize_candidate_valid_rss():
    c = _normalize_candidate({
        "kind": "rss",
        "identifier": "https://example.com/feed.xml",
        "display_name": "Example",
        "reason": "good",
        "confidence": "high",
    })
    assert c is not None
    assert c["kind"] == "rss"
    assert c["confidence"] == "high"


def test_normalize_candidate_strips_at_telegram():
    c = _normalize_candidate({"kind": "telegram", "identifier": "@channel"})
    assert c["identifier"] == "channel"


def test_normalize_candidate_strips_at_twitter():
    c = _normalize_candidate({"kind": "twitter", "identifier": "@elonmusk"})
    assert c["identifier"] == "elonmusk"


def test_normalize_candidate_strips_r_reddit():
    c = _normalize_candidate({"kind": "reddit", "identifier": "r/selfhosted"})
    assert c["identifier"] == "selfhosted"


def test_normalize_candidate_reject_unsupported_kind():
    assert _normalize_candidate({"kind": "facebook", "identifier": "x"}) is None


def test_normalize_candidate_reject_empty_id():
    assert _normalize_candidate({"kind": "rss", "identifier": ""}) is None


def test_normalize_candidate_default_confidence():
    c = _normalize_candidate({"kind": "rss", "identifier": "https://x.com/rss"})
    assert c["confidence"] == "medium"


def test_normalize_candidate_invalid_rss_url():
    assert _normalize_candidate({"kind": "rss", "identifier": "not-a-url"}) is None


async def test_discover_sources_parses_llm_output(monkeypatch):
    fake_response = '''[
  {"kind": "rss", "identifier": "https://www.solidot.org/index.rss", "display_name": "Solidot", "reason": "覆盖科技新闻", "confidence": "high"},
  {"kind": "reddit", "identifier": "selfhosted", "display_name": "r/selfhosted", "reason": "自建服务社区", "confidence": "medium"},
  {"kind": "garbage", "identifier": "x", "confidence": "high"}
]'''
    monkeypatch.setattr(
        "sentinel.services.source_discovery._call_cli",
        lambda prompt, *, model, cli_path, timeout: fake_response)
    config = Config()
    out = await discover_sources(
        name="科技动态", industry="科技",
        monitor_direction="关注 AI 进展",
        config=config)
    # garbage kind 应被过滤
    assert len(out) == 2
    assert out[0]["kind"] == "rss"
    assert out[1]["kind"] == "reddit"


async def test_discover_sources_handles_empty_llm(monkeypatch):
    monkeypatch.setattr(
        "sentinel.services.source_discovery._call_cli",
        lambda prompt, *, model, cli_path, timeout: "")
    config = Config()
    out = await discover_sources(
        name="x", industry="y", monitor_direction="z", config=config)
    assert out == []


async def test_discover_sources_dedupes(monkeypatch):
    fake_response = '''[
  {"kind": "rss", "identifier": "https://a.com/rss", "confidence": "high"},
  {"kind": "rss", "identifier": "https://a.com/rss", "confidence": "low"}
]'''
    monkeypatch.setattr(
        "sentinel.services.source_discovery._call_cli",
        lambda prompt, *, model, cli_path, timeout: fake_response)
    config = Config()
    out = await discover_sources(
        name="x", industry="y", monitor_direction="z", config=config)
    assert len(out) == 1
