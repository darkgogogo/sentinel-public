"""FastAPI dashboard · TestClient 测试。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sentinel.config import Config
from sentinel.web import build_app


@pytest.fixture
def app_client(tmp_db, monkeypatch, tmp_path):
    """构造一个 test app，注入 tmp_db。"""
    monkeypatch.setattr("sentinel.web.routes.KB_ROOT", tmp_path)
    monkeypatch.setattr("sentinel.web.routes.REPORTS_DIR",
                       tmp_path / "01-报告")
    monkeypatch.setattr("sentinel.web.routes.ARCHIVE_DIR",
                       tmp_path / "02-告警归档")
    config = Config()
    app = build_app(config=config, db_path=tmp_db.db_path)
    return TestClient(app)


def test_health(app_client):
    r = app_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_dashboard_empty(app_client):
    r = app_client.get("/")
    assert r.status_code == 200
    assert "Sentinel" in r.text
    assert "Dashboard" in r.text


def test_dashboard_with_data(app_client, tmp_db):
    """有 sources + topic 时 dashboard 显示计数。"""
    import asyncio
    asyncio.run(tmp_db.insert_source("rss", "https://x/rss", "Test Feed"))
    asyncio.run(tmp_db.insert_topic("T", "test", "monitor x"))
    r = app_client.get("/")
    assert r.status_code == 200
    assert "Test Feed" not in r.text  # dashboard 不列具体 source
    # 但计数应该显示
    assert ">1<" in r.text  # 1 source / 1 topic


def test_alerts_page(app_client):
    r = app_client.get("/alerts")
    assert r.status_code == 200
    assert "告警" in r.text


def test_reports_page(app_client):
    r = app_client.get("/reports")
    assert r.status_code == 200
    assert "报告" in r.text


def test_topics_page(app_client, tmp_db):
    import asyncio
    asyncio.run(tmp_db.insert_topic("T1", "VPN", "x"))
    asyncio.run(tmp_db.insert_topic("T2", "教育", "y"))
    r = app_client.get("/topics")
    assert r.status_code == 200
    assert "VPN" in r.text
    assert "教育" in r.text
    assert "T1" in r.text
    assert "T2" in r.text


def test_sources_page(app_client, tmp_db):
    import asyncio
    asyncio.run(tmp_db.insert_source("rss", "https://a/rss", "A"))
    asyncio.run(tmp_db.insert_source("telegram", "@b", "B"))
    r = app_client.get("/sources")
    assert r.status_code == 200
    assert "rss" in r.text
    assert "telegram" in r.text


def test_status_page(app_client, tmp_db):
    import asyncio
    rid = asyncio.run(tmp_db.insert_run("collect", lookback_hours=12.0))
    asyncio.run(tmp_db.update_run(rid, status="success",
                                  messages_collected=5))
    r = app_client.get("/status")
    assert r.status_code == 200
    assert "success" in r.text
    assert "collect" in r.text


def test_report_view_404_on_missing(app_client):
    r = app_client.get("/reports/view?rel=nonexistent.md")
    assert r.status_code == 404


def test_report_view_400_on_path_traversal(app_client):
    r = app_client.get("/reports/view?rel=../../../etc/passwd")
    assert r.status_code in (400, 404)


def test_report_view_renders_markdown(app_client, tmp_path):
    # 在 mocked KB 下写个测试 md
    md_path = tmp_path / "01-报告" / "测试.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        "---\ntags:\n  - test\n---\n\n# Hello\n\nThis is **bold**.")
    r = app_client.get(f"/reports/view?rel=01-报告/测试.md")
    assert r.status_code == 200
    assert "Hello" in r.text
    assert "<strong>bold</strong>" in r.text
