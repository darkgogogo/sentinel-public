"""kb.py · 02-告警归档/ 和 03-主题/[topic]/信源.md 写入测试。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from sentinel.kb import (
    append_alert_archive, ensure_topic_sources_file,
    topic_slug, _week_id,
)
from sentinel.triage import TriageVerdict


def test_week_id_iso_format():
    dt = datetime(2026, 5, 15)
    wid = _week_id(dt)
    assert wid.startswith("2026-W")
    # 2026-05-15 是 ISO Week 20
    assert wid == "2026-W20"


def test_topic_slug_safe_chars():
    assert topic_slug("my-product") == "my-product"
    assert topic_slug("协议 封锁") == "协议-封锁"
    assert topic_slug("a/b\\c") == "a-b-c"


def test_append_alert_archive_creates_weekly_file(tmp_path):
    topic = {"name": "Test", "industry": "VPN"}
    verdict = TriageVerdict(
        worth_alert=True, headline="测试事件",
        summary="一段摘要\n第二行",
        related_message_ids=[1],
    )
    messages = [{
        "id": 1, "source_display_name": "Solidot",
        "author": "alice", "content": "证据原文",
        "url": "https://example.com/1",
        "posted_at": "2026-05-15T10:00:00",
    }]
    path = append_alert_archive(topic, verdict, messages,
                               archive_root=tmp_path)
    assert path.exists()
    content = path.read_text()
    # frontmatter
    assert "sentinel/alert-archive" in content
    # header
    assert "🚨 测试事件" in content
    # blockquote summary
    assert "> 一段摘要" in content
    # 元信息表
    assert "| Test | VPN |" in content
    # 证据
    assert "Solidot" in content
    assert "alice" in content
    assert "证据原文" in content
    assert "https://example.com/1" in content


def test_append_alert_archive_appends_to_existing_file(tmp_path):
    topic = {"name": "T", "industry": "x"}
    verdict1 = TriageVerdict(worth_alert=True, headline="A",
                             summary="s1", related_message_ids=[1])
    verdict2 = TriageVerdict(worth_alert=True, headline="B",
                             summary="s2", related_message_ids=[1])
    messages = [{"id": 1, "source_display_name": "src",
                 "author": "u", "content": "c", "url": None,
                 "posted_at": "2026-05-15"}]

    p1 = append_alert_archive(topic, verdict1, messages, archive_root=tmp_path)
    p2 = append_alert_archive(topic, verdict2, messages, archive_root=tmp_path)
    assert p1 == p2  # 同一周
    content = p2.read_text()
    assert "🚨 A" in content
    assert "🚨 B" in content


def test_ensure_topic_sources_file_creates_template(tmp_path):
    topic = {
        "name": "my-product", "industry": "VPN",
        "alert_enabled": 1,
        "monitor_direction": "my-product 自己 + 直接竞品",
    }
    sources = [
        {"kind": "telegram", "identifier": "@example_channel",
         "display_name": "GFW Knocker"},
        {"kind": "rss", "identifier": "https://x/rss",
         "display_name": "X RSS"},
    ]
    p = ensure_topic_sources_file(topic, sources, topic_root=tmp_path)
    assert p.exists()
    content = p.read_text()
    assert "sentinel/topic-sources" in content
    assert "topic: my-product" in content
    assert "industry: VPN" in content
    assert "## 信源清单" in content
    assert "@example_channel" in content
    assert "## advisor 建议" in content


def test_ensure_topic_sources_file_idempotent(tmp_path):
    """已存在时不覆盖。"""
    topic = {"name": "x", "industry": "g", "monitor_direction": ""}
    p = ensure_topic_sources_file(topic, [], topic_root=tmp_path)
    p.write_text("CUSTOM CONTENT")
    p2 = ensure_topic_sources_file(topic, [], topic_root=tmp_path)
    assert p2.read_text() == "CUSTOM CONTENT"
