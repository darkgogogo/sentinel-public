"""db.py schema + 基础 CRUD 测试。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import aiosqlite


async def test_init_schema_creates_all_tables(tmp_db):
    expected = {"sources", "topics", "topic_keywords", "topic_sources",
                "messages", "alerts", "service_runs"}
    async with aiosqlite.connect(tmp_db.db_path) as db:
        async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'") as cur:
            tables = {r[0] for r in await cur.fetchall()}
    assert expected.issubset(tables)


async def test_topics_has_industry_and_alert_enabled_columns(tmp_db):
    """v2 关键字段：industry + alert_enabled。"""
    async with aiosqlite.connect(tmp_db.db_path) as db:
        async with db.execute("PRAGMA table_info(topics)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
    assert "industry" in cols
    assert "alert_enabled" in cols


async def test_service_runs_has_service_column(tmp_db):
    """v2: daemon_runs → service_runs，加 service 字段。"""
    async with aiosqlite.connect(tmp_db.db_path) as db:
        async with db.execute("PRAGMA table_info(service_runs)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
    assert "service" in cols


async def test_insert_and_query_source(tmp_db):
    sid = await tmp_db.insert_source("telegram", "@test", "Test Channel")
    assert sid > 0
    sources = await tmp_db.list_enabled_sources()
    assert len(sources) == 1
    assert sources[0]["kind"] == "telegram"
    assert sources[0]["identifier"] == "@test"
    assert sources[0]["enabled"] == 1
    assert sources[0]["failure_count"] == 0


async def test_message_unique_dedup(tmp_db):
    sid = await tmp_db.insert_source("rss", "https://example.com/rss")
    posted = datetime.now(timezone.utc)
    m1 = await tmp_db.insert_message(sid, "ext-1", None, "content", None, posted)
    m2 = await tmp_db.insert_message(sid, "ext-1", None, "content", None, posted)
    assert m1 is not None
    assert m2 is None  # 重复 external_id 返回 None


async def test_failure_count_increment_and_reset(tmp_db):
    sid = await tmp_db.insert_source("rss", "https://example.com/bad")
    c1 = await tmp_db.increment_failure(sid)
    c2 = await tmp_db.increment_failure(sid)
    assert c1 == 1 and c2 == 2
    await tmp_db.reset_failure(sid)
    sources = await tmp_db.list_enabled_sources()
    assert sources[0]["failure_count"] == 0


async def test_service_runs_round_trip(tmp_db):
    run_id = await tmp_db.insert_run(service="collect", lookback_hours=48.0)
    await tmp_db.update_run(run_id, status="success", messages_collected=42)
    last = await tmp_db.last_successful_run("collect")
    assert last is not None
    assert last["messages_collected"] == 42
    assert last["status"] == "success"


async def test_last_successful_run_isolated_per_service(tmp_db):
    """collect 的 success 不污染 alert 的 last_successful。"""
    rid = await tmp_db.insert_run("collect")
    await tmp_db.update_run(rid, status="success")
    last_alert = await tmp_db.last_successful_run("alert")
    assert last_alert is None


# §19 D: alert_enabled flag on topic_sources
async def test_link_topic_source_default_alert_enabled(tmp_db):
    """新链接默认 alert_enabled=1（向后兼容既有用法）。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("T", "g", "x")
    await tmp_db.link_topic_source(tid, sid)
    sources = await tmp_db.list_topic_sources(tid)
    assert len(sources) == 1
    assert sources[0]["alert_enabled"] == 1


async def test_link_topic_source_with_alert_disabled(tmp_db):
    """alert_enabled=False 时 alert_only 拉到 0 个，普通拉到 1 个。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("T", "g", "x")
    await tmp_db.link_topic_source(tid, sid, alert_enabled=False)
    # advisor / analyze 看得到
    all_src = await tmp_db.list_topic_sources(tid)
    assert len(all_src) == 1
    assert all_src[0]["alert_enabled"] == 0
    # alert service 看不到
    alert_src = await tmp_db.list_topic_sources(tid, alert_only=True)
    assert alert_src == []


async def test_list_topic_sources_alert_only_filters_mixed(tmp_db):
    """两个 source 一开一关，alert_only 只返回开的那个。"""
    sid_on = await tmp_db.insert_source("rss", "https://on/rss")
    sid_off = await tmp_db.insert_source("rss", "https://off/rss")
    tid = await tmp_db.insert_topic("T", "g", "x")
    await tmp_db.link_topic_source(tid, sid_on, alert_enabled=True)
    await tmp_db.link_topic_source(tid, sid_off, alert_enabled=False)
    alert_src = await tmp_db.list_topic_sources(tid, alert_only=True)
    assert len(alert_src) == 1
    assert alert_src[0]["id"] == sid_on
