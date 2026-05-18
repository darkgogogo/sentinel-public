"""push.py · MarkdownV2 转义 + 格式化 + mock httpx。"""
from __future__ import annotations

import pytest

from sentinel.config import Config
from sentinel.push import _escape_md, _format_telegram_text, send_telegram
from sentinel.triage import TriageVerdict


def test_escape_md_basic_special_chars():
    assert _escape_md("hello.world!") == "hello\\.world\\!"
    assert _escape_md("a*b_c") == "a\\*b\\_c"
    assert _escape_md("(1)") == "\\(1\\)"


def test_escape_md_preserves_chinese():
    assert _escape_md("中文测试") == "中文测试"


def test_format_telegram_text_includes_headline_and_summary():
    verdict = TriageVerdict(
        worth_alert=True,
        headline="测试事件",
        summary="发生了一件事",
        related_message_ids=[1],
    )
    messages = [{
        "id": 1, "source_display_name": "src-a",
        "author": "alice", "content": "evidence content",
        "url": "https://example.com/1", "posted_at": "2026-05-15",
    }]
    text = _format_telegram_text("Test Topic", verdict, messages)
    assert "测试事件" in text
    assert "发生了一件事" in text
    assert "src\\-a" in text  # source display escaped
    assert "alice" in text
    assert "evidence content" in text
    assert "Test Topic" in text


def test_format_groups_evidence_by_source():
    verdict = TriageVerdict(
        worth_alert=True, headline="h", summary="s",
        related_message_ids=[1, 2, 3])
    messages = [
        {"id": 1, "source_display_name": "A", "author": "u1",
         "content": "msg1", "url": None, "posted_at": ""},
        {"id": 2, "source_display_name": "A", "author": "u2",
         "content": "msg2", "url": None, "posted_at": ""},
        {"id": 3, "source_display_name": "B", "author": "u3",
         "content": "msg3", "url": None, "posted_at": ""},
    ]
    text = _format_telegram_text("T", verdict, messages)
    # 跨 2 个频道
    assert "跨 2 个频道" in text
    # 两个 source 块都在
    assert text.count("🔹") == 2


async def test_send_telegram_dry_run_returns_true(caplog):
    config = Config()
    config.secrets["TELEGRAM_BOT_TOKEN"] = "dummy"
    config.secrets["TELEGRAM_CHAT_ID"] = "123"
    verdict = TriageVerdict(worth_alert=True, headline="h", summary="s",
                            related_message_ids=[])
    ok = await send_telegram("T", verdict, [], config, dry_run=True)
    assert ok is True


async def test_send_telegram_returns_false_without_creds():
    config = Config()
    # 不设置 BOT_TOKEN / CHAT_ID
    verdict = TriageVerdict(worth_alert=True, headline="h", summary="s",
                            related_message_ids=[])
    ok = await send_telegram("T", verdict, [], config, dry_run=False)
    assert ok is False
