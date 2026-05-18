"""D2 写入 + 运维操作测试。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from sentinel.config import Config
from sentinel.web import build_app


@pytest.fixture
def app_client(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setattr("sentinel.web.routes.KB_ROOT", tmp_path)
    monkeypatch.setattr("sentinel.web.routes.REPORTS_DIR",
                       tmp_path / "01-报告")
    monkeypatch.setattr("sentinel.web.routes.ARCHIVE_DIR",
                       tmp_path / "02-告警归档")
    monkeypatch.setattr("sentinel.web.routes.PROJECT_ROOT", tmp_path)
    config = Config()
    app = build_app(config=config, db_path=tmp_db.db_path)
    return TestClient(app)


# ---------- sources 写入 ----------


def test_sources_add_skip_resolve(app_client, tmp_db):
    r = app_client.post("/sources/add", data={
        "kind": "rss", "identifier": "https://example.com/rss",
        "display_name": "Example", "skip_resolve": "true",
    }, follow_redirects=False)
    assert r.status_code == 303
    sources = asyncio.run(tmp_db.list_enabled_sources())
    assert len(sources) == 1
    assert sources[0]["identifier"] == "https://example.com/rss"
    assert sources[0]["display_name"] == "Example"


def test_sources_add_rejects_unknown_kind(app_client):
    r = app_client.post("/sources/add", data={
        "kind": "invalid", "identifier": "x", "skip_resolve": "true",
    })
    assert r.status_code == 400


def test_sources_add_rejects_duplicate(app_client, tmp_db):
    asyncio.run(tmp_db.insert_source("rss", "https://x/rss"))
    r = app_client.post("/sources/add", data={
        "kind": "rss", "identifier": "https://x/rss",
        "skip_resolve": "true",
    })
    assert r.status_code == 400


def test_sources_toggle(app_client, tmp_db):
    sid = asyncio.run(tmp_db.insert_source("rss", "https://x/rss"))
    r = app_client.post(f"/sources/{sid}/toggle", follow_redirects=False)
    assert r.status_code == 303
    sources = asyncio.run(tmp_db.list_enabled_sources())
    assert len(sources) == 0  # 已 disable

    app_client.post(f"/sources/{sid}/toggle", follow_redirects=False)
    sources = asyncio.run(tmp_db.list_enabled_sources())
    assert len(sources) == 1  # 再 toggle 恢复


def test_sources_delete(app_client, tmp_db):
    sid = asyncio.run(tmp_db.insert_source("rss", "https://x/rss"))
    r = app_client.post(f"/sources/{sid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert asyncio.run(tmp_db.get_source(sid)) is None


def test_sources_delete_cascades_messages(app_client, tmp_db):
    """有 messages 的 source 删除 → source + messages 全清（不再 FK 500）。"""
    sid = asyncio.run(tmp_db.insert_source("rss", "https://x/rss"))
    asyncio.run(tmp_db.insert_message(
        source_id=sid, external_id="m1", author=None, content="x",
        url=None, posted_at=datetime.now(timezone.utc)))
    asyncio.run(tmp_db.insert_message(
        source_id=sid, external_id="m2", author=None, content="y",
        url=None, posted_at=datetime.now(timezone.utc)))

    r = app_client.post(f"/sources/{sid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert asyncio.run(tmp_db.get_source(sid)) is None
    # messages 跟着删
    msgs = asyncio.run(tmp_db.list_messages_by_source(sid)) \
        if hasattr(tmp_db, "list_messages_by_source") else None
    if msgs is None:
        # fallback: 直接 raw query
        import aiosqlite

        async def _count():
            async with aiosqlite.connect(tmp_db.db_path) as db:
                cur = await db.execute(
                    "SELECT COUNT(*) FROM messages WHERE source_id=?", (sid,))
                row = await cur.fetchone()
                return row[0]
        msgs_n = asyncio.run(_count())
        assert msgs_n == 0
    else:
        assert msgs == []


# ---------- topics 写入 ----------


def test_topics_add(app_client, tmp_db):
    r = app_client.post("/topics/add", data={
        "name": "T1", "industry": "VPN",
        "monitor_direction": "x", "alert_enabled": "true",
    }, follow_redirects=False)
    assert r.status_code == 303
    topics = asyncio.run(tmp_db.list_all_topics())
    assert len(topics) == 1
    assert topics[0]["name"] == "T1"
    assert topics[0]["industry"] == "VPN"
    assert topics[0]["alert_enabled"] == 1


def test_topics_toggle_alert(app_client, tmp_db):
    tid = asyncio.run(tmp_db.insert_topic("T", "g", "x"))
    r = app_client.post(f"/topics/{tid}/toggle-alert", follow_redirects=False)
    assert r.status_code == 303
    t = asyncio.run(tmp_db.get_topic(tid))
    assert t["alert_enabled"] == 0


def test_topics_link_and_unlink(app_client, tmp_db):
    sid = asyncio.run(tmp_db.insert_source("rss", "https://x/rss"))
    tid = asyncio.run(tmp_db.insert_topic("T", "g", "x"))
    r = app_client.post(f"/topics/{tid}/link", data={
        "source_id": str(sid),
    }, follow_redirects=False)
    assert r.status_code == 303
    linked = asyncio.run(tmp_db.list_topic_sources(tid))
    assert len(linked) == 1 and linked[0]["id"] == sid

    r = app_client.post(f"/topics/{tid}/unlink", data={
        "source_id": str(sid),
    }, follow_redirects=False)
    linked = asyncio.run(tmp_db.list_topic_sources(tid))
    assert len(linked) == 0


def test_topics_add_keyword_remove(app_client, tmp_db):
    tid = asyncio.run(tmp_db.insert_topic("T", "g", "x"))
    app_client.post(f"/topics/{tid}/keyword", data={"keyword": "VPN"},
                    follow_redirects=False)
    app_client.post(f"/topics/{tid}/keyword", data={"keyword": "GFW"},
                    follow_redirects=False)
    kws = asyncio.run(tmp_db.list_topic_keywords(tid))
    assert set(kws) == {"VPN", "GFW"}

    app_client.post(f"/topics/{tid}/keyword/remove", data={"keyword": "VPN"},
                    follow_redirects=False)
    kws = asyncio.run(tmp_db.list_topic_keywords(tid))
    assert kws == ["GFW"]


def test_topics_delete_cascades(app_client, tmp_db):
    sid = asyncio.run(tmp_db.insert_source("rss", "https://x/rss"))
    tid = asyncio.run(tmp_db.insert_topic("T", "g", "x"))
    asyncio.run(tmp_db.link_topic_source(tid, sid))
    asyncio.run(tmp_db.add_topic_keyword(tid, "k"))

    app_client.post(f"/topics/{tid}/delete", follow_redirects=False)
    assert asyncio.run(tmp_db.get_topic(tid)) is None
    # FK cascade 应该清掉 topic_keywords / topic_sources（但 source 仍在）
    assert asyncio.run(tmp_db.get_source(sid)) is not None


def test_topics_delete_keeps_alerts_as_orphans(app_client, tmp_db):
    """有 alerts 的 topic 删除 → topic 没了，alert 保留为孤儿 (topic_id=NULL)。"""
    tid = asyncio.run(tmp_db.insert_topic("T2", "g", "x"))
    aid = asyncio.run(tmp_db.insert_alert(
        topic_id=tid, headline="h", summary="s",
        related_message_ids=[], push_status="pending"))

    r = app_client.post(f"/topics/{tid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert asyncio.run(tmp_db.get_topic(tid)) is None
    # alert 保留为孤儿
    import aiosqlite

    async def _get_alert():
        async with aiosqlite.connect(tmp_db.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, topic_id FROM alerts WHERE id=?", (aid,))
            return await cur.fetchone()
    row = asyncio.run(_get_alert())
    assert row is not None
    assert row["topic_id"] is None


def test_topic_detail_page(app_client, tmp_db):
    tid = asyncio.run(tmp_db.insert_topic("MyTopic", "VPN", "monitor x"))
    asyncio.run(tmp_db.add_topic_keyword(tid, "kw1"))
    r = app_client.get(f"/topics/{tid}")
    assert r.status_code == 200
    assert "MyTopic" in r.text
    assert "monitor x" in r.text
    assert "kw1" in r.text


def test_topic_detail_404(app_client):
    r = app_client.get("/topics/9999")
    assert r.status_code == 404


# ---------- 运维 ----------


def test_service_pause_and_resume(app_client, tmp_path, monkeypatch):
    r = app_client.post("/services/collect/pause", data={"days": "0"},
                        follow_redirects=False)
    assert r.status_code == 303
    assert (tmp_path / ".pause-collect").exists()

    r = app_client.post("/services/collect/resume", follow_redirects=False)
    assert r.status_code == 303
    assert not (tmp_path / ".pause-collect").exists()


def test_service_pause_rejects_invalid_service(app_client):
    r = app_client.post("/services/bogus/pause", data={"days": "0"})
    assert r.status_code == 400


def test_service_run_analyze_requires_topic(app_client):
    r = app_client.post("/services/analyze/run", data={
        "period_hours": "168", "mode": "auto",
    })
    assert r.status_code == 400


# ---------- alert 标记 ----------


def test_alert_label_true_positive(app_client, tmp_db):
    tid = asyncio.run(tmp_db.insert_topic("T", "g", "x"))
    aid = asyncio.run(tmp_db.insert_alert(
        tid, "h", "s", [], push_status="sent"))
    r = app_client.post(f"/alerts/{aid}/label",
                        data={"label": "true_positive"},
                        follow_redirects=False)
    assert r.status_code == 303
    alerts = asyncio.run(tmp_db.recent_alerts(limit=5))
    assert alerts[0]["user_label"] == "true_positive"


def test_alert_label_clear(app_client, tmp_db):
    tid = asyncio.run(tmp_db.insert_topic("T", "g", "x"))
    aid = asyncio.run(tmp_db.insert_alert(tid, "h", "s", []))
    asyncio.run(tmp_db.label_alert(aid, "false_positive"))
    app_client.post(f"/alerts/{aid}/label",
                    data={"label": "clear"},
                    follow_redirects=False)
    alerts = asyncio.run(tmp_db.recent_alerts(limit=5))
    assert alerts[0]["user_label"] is None


def test_alert_label_rejects_invalid(app_client, tmp_db):
    tid = asyncio.run(tmp_db.insert_topic("T", "g", "x"))
    aid = asyncio.run(tmp_db.insert_alert(tid, "h", "s", []))
    r = app_client.post(f"/alerts/{aid}/label",
                        data={"label": "bogus"})
    assert r.status_code == 400
