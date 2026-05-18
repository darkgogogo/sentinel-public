"""coverage_audit service · 测试指标计算 + severity 分类 + LLM 解析。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sentinel.config import Config
from sentinel.services.coverage_audit import (
    DEFAULT_THRESHOLDS, TopicMetrics, _classify_severity, _extract_json_obj,
    _gather_metrics, audit_topic, run_coverage_audit,
)


# ===== severity 分类 =====


def _make_metrics(**kw):
    """快捷构造 TopicMetrics。"""
    defaults = {
        "topic_id": 1, "topic_name": "T", "industry": "test",
        "monitor_direction": "x",
        "msgs_7d": 500, "platform_count": 4, "source_count": 5,
        "failure_pct": 0.0, "alerts_7d": 5, "signal_ratio_pct": 1.0,
        "sources": [],
    }
    defaults.update(kw)
    return TopicMetrics(**defaults)


def test_severity_ok_when_all_healthy():
    m = _make_metrics()
    assert _classify_severity(m) == "ok"


def test_severity_high_when_no_sources():
    m = _make_metrics(source_count=0, msgs_7d=0)
    assert _classify_severity(m) == "high"


def test_severity_high_when_msgs_below_30():
    m = _make_metrics(msgs_7d=10)
    assert _classify_severity(m) == "high"


def test_severity_high_when_only_one_platform():
    m = _make_metrics(platform_count=1)
    assert _classify_severity(m) == "high"


def test_severity_high_when_failure_above_50pct():
    m = _make_metrics(failure_pct=60.0)
    assert _classify_severity(m) == "high"


def test_severity_medium_when_msgs_below_100():
    m = _make_metrics(msgs_7d=60)
    assert _classify_severity(m) == "medium"


def test_severity_medium_when_two_platforms():
    m = _make_metrics(platform_count=2)
    assert _classify_severity(m) == "medium"


def test_severity_medium_when_failure_above_30pct():
    m = _make_metrics(failure_pct=40.0)
    assert _classify_severity(m) == "medium"


def test_severity_high_takes_precedence_over_medium():
    """msgs<30 (high) + platform=2 (medium) → high"""
    m = _make_metrics(msgs_7d=10, platform_count=2)
    assert _classify_severity(m) == "high"


# ===== JSON 提取 =====


def test_extract_json_obj_plain():
    assert _extract_json_obj('{"a": 1}') == {"a": 1}


def test_extract_json_obj_with_fence():
    text = '```json\n{"diagnosis_md": "x", "rsshub_suggestions": []}\n```'
    out = _extract_json_obj(text)
    assert out["diagnosis_md"] == "x"


def test_extract_json_obj_with_preamble():
    text = '基于以下指标，给你的建议是：\n{"diagnosis_md": "x"}\n希望有帮助'
    out = _extract_json_obj(text)
    assert out["diagnosis_md"] == "x"


def test_extract_json_obj_invalid():
    assert _extract_json_obj("") == {}
    assert _extract_json_obj("not json") == {}
    assert _extract_json_obj("[1,2,3]") == {}  # 是 list 不是 dict


# ===== service e2e =====


async def test_gather_metrics_no_sources(tmp_db):
    tid = await tmp_db.insert_topic("T", "test", "x")
    topic = await tmp_db.get_topic(tid)
    m = await _gather_metrics(tmp_db, topic)
    assert m.msgs_7d == 0
    assert m.platform_count == 0
    assert m.source_count == 0


async def test_gather_metrics_with_sources_and_messages(tmp_db):
    tid = await tmp_db.insert_topic("T", "test", "x")
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.insert_message(
        sid, "msg-1", "u", "content", None,
        datetime.now(timezone.utc))
    topic = await tmp_db.get_topic(tid)
    m = await _gather_metrics(tmp_db, topic)
    assert m.source_count == 1
    assert m.platform_count == 1
    assert m.msgs_7d == 1


async def test_audit_topic_skip_llm_writes_record(tmp_db):
    tid = await tmp_db.insert_topic("Lonely", "test", "monitor x")
    topic = await tmp_db.get_topic(tid)
    result = await audit_topic(tmp_db, topic, config=Config(), skip_llm=True)
    assert result["severity"] == "high"  # 无源 = high
    assert "audit_id" in result

    # latest_coverage_audit 应能查到
    latest = await tmp_db.latest_coverage_audit(tid)
    assert latest is not None
    assert latest["severity"] == "high"
    assert latest["status"] == "pending"


async def test_audit_topic_ok_does_not_write_record(tmp_db, monkeypatch):
    """severity=ok 时不写 audit 记录（省 db）。"""
    sid = await tmp_db.insert_source("rss", "https://a/rss")
    sid2 = await tmp_db.insert_source("telegram", "@x")
    sid3 = await tmp_db.insert_source("twitter", "elonmusk")
    tid = await tmp_db.insert_topic("Healthy", "test", "x")
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.link_topic_source(tid, sid2)
    await tmp_db.link_topic_source(tid, sid3)
    # 插 200 条消息
    for i in range(200):
        await tmp_db.insert_message(
            sid, f"m-{i}", "u", "c", None, datetime.now(timezone.utc))
    # 插 5 条 alert
    for i in range(5):
        await tmp_db.insert_alert(
            topic_id=tid, headline=f"h-{i}", summary="s",
            related_message_ids=[], push_status="sent",
            push_channels=["telegram"])

    topic = await tmp_db.get_topic(tid)
    result = await audit_topic(tmp_db, topic, config=Config(), skip_llm=True)
    assert result["severity"] == "ok"
    assert "audit_id" not in result
    latest = await tmp_db.latest_coverage_audit(tid)
    assert latest is None  # 没写


async def test_run_coverage_audit_full(tmp_db):
    """完整跑一遍 audit，返 list[(topic, severity)]。"""
    await tmp_db.insert_topic("A", "test", "x")
    await tmp_db.insert_topic("B", "test", "y")
    results = await run_coverage_audit(Config(), tmp_db, skip_llm=True)
    assert len(results) == 2
    assert all(r["severity"] == "high" for r in results)  # 都无源


# ===== dismiss / execute 流转 =====


async def test_dismiss_audit_updates_status(tmp_db):
    tid = await tmp_db.insert_topic("T", "test", "x")
    aid = await tmp_db.insert_coverage_audit(
        topic_id=tid, severity="high", metrics={"msgs_7d": 0})
    from datetime import timedelta
    until = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    await tmp_db.dismiss_coverage_audit(aid, until)
    latest = await tmp_db.latest_coverage_audit(tid)
    assert latest["status"] == "dismissed"
    assert latest["dismissed_until"] == until


async def test_execute_audit_updates_status(tmp_db):
    tid = await tmp_db.insert_topic("T", "test", "x")
    aid = await tmp_db.insert_coverage_audit(
        topic_id=tid, severity="medium", metrics={})
    await tmp_db.execute_coverage_audit(aid, "added 3 rss sources")
    latest = await tmp_db.latest_coverage_audit(tid)
    assert latest["status"] == "executed"
    assert latest["notes"] == "added 3 rss sources"


async def test_pending_audits_filter_dismissed_until(tmp_db):
    tid = await tmp_db.insert_topic("T", "test", "x")
    aid_active = await tmp_db.insert_coverage_audit(
        topic_id=tid, severity="high", metrics={})
    aid_dismissed_expired = await tmp_db.insert_coverage_audit(
        topic_id=tid, severity="medium", metrics={})
    # 一条 pending 但 dismissed_until 在过去（应该重新出现）
    from datetime import timedelta
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    # 但 dismiss 同时改 status=dismissed，所以这条不算 pending
    await tmp_db.dismiss_coverage_audit(aid_dismissed_expired, past)

    pending = await tmp_db.pending_coverage_audits()
    pending_ids = {p["id"] for p in pending}
    assert aid_active in pending_ids
    assert aid_dismissed_expired not in pending_ids  # status='dismissed'


# ===== file_inbox collector =====


def test_file_inbox_parse_frontmatter():
    from sentinel.collectors.file_inbox import _parse_frontmatter
    text = """---
platform: 知乎
source_url: https://www.zhihu.com/x
collected_at: 2026-05-16
---

正文内容
更多内容"""
    meta, body = _parse_frontmatter(text)
    assert meta["platform"] == "知乎"
    assert meta["source_url"] == "https://www.zhihu.com/x"
    assert "正文内容" in body


def test_file_inbox_parse_no_frontmatter():
    from sentinel.collectors.file_inbox import _parse_frontmatter
    meta, body = _parse_frontmatter("just plain markdown")
    assert meta == {}
    assert body == "just plain markdown"


async def test_inbox_collector_ingest_and_mark(tmp_path, monkeypatch):
    """InboxCollector 抓 inbox/<slug>/*.md, ingest 后改名 .ingested 防重复."""
    from sentinel.collectors.file_inbox import InboxCollector

    inbox_root = tmp_path / "inbox"
    slug_dir = inbox_root / "TestTopic"
    slug_dir.mkdir(parents=True)
    md = slug_dir / "sample.md"
    md.write_text(
        "---\nplatform: zhihu\nsource_url: https://x.com/1\n"
        "collected_at: 2026-05-15\n---\n\n这是抓回来的正文",
        encoding="utf-8")

    monkeypatch.setattr("sentinel.collectors.file_inbox.INBOX_ROOT", inbox_root)

    coll = InboxCollector(
        source_row={"id": 1, "identifier": "TestTopic"}, config={})
    msgs = []
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async for m in coll.collect(since=since):
        msgs.append(m)
    assert len(msgs) == 1
    assert "正文" in msgs[0].content
    assert msgs[0].raw_json["platform"] == "zhihu"

    # 应已改名为 .ingested
    assert not md.exists()
    assert (slug_dir / "sample.md.ingested").exists()

    # 第二次跑应该跳过（防重复）
    coll2 = InboxCollector(
        source_row={"id": 1, "identifier": "TestTopic"}, config={})
    msgs2 = []
    async for m in coll2.collect(since=since):
        msgs2.append(m)
    assert len(msgs2) == 0
