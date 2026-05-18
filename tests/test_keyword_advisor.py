"""keyword_advisor · 推荐关键词测试."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sentinel.config import Config
from sentinel.services.keyword_advisor import (
    _extract_json_list, _format_messages_for_prompt, _normalize_candidate,
    recommend_keywords,
)


def test_extract_json_list_with_fence():
    text = '''Sure, here are recommendations:
```json
[
  {"keyword": "VLESS", "reason": "x", "confidence": "high"}
]
```'''
    out = _extract_json_list(text)
    assert len(out) == 1
    assert out[0]["keyword"] == "VLESS"


def test_extract_json_list_plain_array():
    out = _extract_json_list('[{"keyword":"x","reason":"y"}]')
    assert len(out) == 1


def test_extract_json_list_invalid():
    assert _extract_json_list("") == []
    assert _extract_json_list("not json") == []
    assert _extract_json_list('{"obj": true}') == []  # 不是 list


def test_normalize_candidate_valid():
    c = _normalize_candidate(
        {"keyword": "VLESS", "reason": "x", "confidence": "high"}, set())
    assert c["keyword"] == "VLESS"
    assert c["confidence"] == "high"


def test_normalize_candidate_default_confidence():
    c = _normalize_candidate({"keyword": "xy"}, set())
    assert c["confidence"] == "medium"


def test_normalize_candidate_filter_too_short():
    assert _normalize_candidate({"keyword": "a"}, set()) is None


def test_normalize_candidate_filter_too_long():
    long_kw = "a" * 31
    assert _normalize_candidate({"keyword": long_kw}, set()) is None


def test_normalize_candidate_filter_existing_case_insensitive():
    """已有 'VLESS' (lower) → 推荐的 'vless' 应被过滤。"""
    existing = {"vless"}
    assert _normalize_candidate({"keyword": "VLESS"}, existing) is None
    assert _normalize_candidate({"keyword": "vless"}, existing) is None


def test_normalize_candidate_passes_when_not_existing():
    existing = {"vpn"}
    c = _normalize_candidate({"keyword": "VLESS"}, existing)
    assert c is not None


def test_format_messages_empty():
    assert "无消息样本" in _format_messages_for_prompt([])


def test_format_messages_with_alert_marker():
    msgs = [
        {"content": "事件 A", "source_kind": "rss", "alert_id": 1},
        {"content": "事件 B", "source_kind": "telegram", "alert_id": None},
    ]
    out = _format_messages_for_prompt(msgs)
    assert "⭐" in out  # alert 命中标记
    assert "[rss]" in out
    assert "[telegram]" in out


def test_format_messages_truncate_long():
    msgs = [{"content": "x" * 500, "source_kind": "rss"}]
    out = _format_messages_for_prompt(msgs, max_content_chars=100)
    assert "…" in out
    # 截断后内容应 < 500
    assert len(out) < 200


# ===== e2e (mock LLM) =====


async def test_recommend_keywords_no_topic_returns_error(tmp_db):
    out = await recommend_keywords(99999, tmp_db, config=Config())
    assert out.get("error") == "topic 不存在"


async def test_recommend_keywords_filters_existing(tmp_db, monkeypatch):
    """已有 'VLESS' 时，LLM 推 'VLESS' 应被过滤掉。"""
    tid = await tmp_db.insert_topic("T", "test", "x")
    await tmp_db.add_topic_keyword(tid, "VLESS")

    fake = '''[
      {"keyword": "VLESS", "reason": "已有", "confidence": "high"},
      {"keyword": "Reality", "reason": "新的", "confidence": "high"}
    ]'''
    monkeypatch.setattr(
        "sentinel.services.keyword_advisor._call_cli",
        lambda prompt, *, model, cli_path, timeout: fake)

    result = await recommend_keywords(tid, tmp_db, config=Config())
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["keyword"] == "Reality"


async def test_recommend_keywords_dedupe_within_response(tmp_db, monkeypatch):
    """LLM 返回重复时，去重保留首条。"""
    tid = await tmp_db.insert_topic("T", "test", "x")
    fake = '''[
      {"keyword": "VLESS", "reason": "first", "confidence": "high"},
      {"keyword": "VLESS", "reason": "duplicate", "confidence": "low"}
    ]'''
    monkeypatch.setattr(
        "sentinel.services.keyword_advisor._call_cli",
        lambda prompt, *, model, cli_path, timeout: fake)
    result = await recommend_keywords(tid, tmp_db, config=Config())
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["reason"] == "first"


async def test_recommend_keywords_handles_empty_llm(tmp_db, monkeypatch):
    tid = await tmp_db.insert_topic("T", "test", "x")
    monkeypatch.setattr(
        "sentinel.services.keyword_advisor._call_cli",
        lambda *a, **kw: "")
    result = await recommend_keywords(tid, tmp_db, config=Config())
    assert result["candidates"] == []
    assert "error" in result


async def test_recommend_keywords_with_message_samples(tmp_db, monkeypatch):
    """有消息样本时，应该传入 LLM prompt (verify prompt 含样本内容)。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("T", "test", "VPN 监控")
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.insert_message(
        sid, "ext-1", "u", "VLESS 协议被全面封锁", None,
        datetime.now(timezone.utc))

    captured_prompts = []

    def capture_prompt(prompt, *, model, cli_path, timeout):
        captured_prompts.append(prompt)
        return '[{"keyword": "封锁", "reason": "见样本", "confidence": "high"}]'

    monkeypatch.setattr(
        "sentinel.services.keyword_advisor._call_cli", capture_prompt)
    result = await recommend_keywords(tid, tmp_db, config=Config())
    assert len(captured_prompts) == 1
    assert "VLESS 协议被全面封锁" in captured_prompts[0]
    assert len(result["candidates"]) == 1
