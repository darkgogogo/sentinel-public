"""v1 → v2 配置迁移测试。"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sentinel.migrate import migrate, format_report


@pytest.fixture
def v1_db(tmp_path) -> Path:
    """构造一个 mock v1 sqlite（schema 跟 v1 一致）。"""
    p = tmp_path / "v1.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            identifier TEXT NOT NULL,
            display_name TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(kind, identifier)
        );
        CREATE TABLE topics (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            monitor_direction TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE topic_keywords (
            id INTEGER PRIMARY KEY,
            topic_id INTEGER NOT NULL,
            keyword TEXT NOT NULL
        );
        CREATE TABLE topic_sources (
            topic_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            skip_keyword_filter INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (topic_id, source_id)
        );
    """)
    now = datetime.now(timezone.utc).isoformat()

    conn.executemany(
        "INSERT INTO sources (kind, identifier, display_name, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("telegram", "@example_channel", "GFW Knocker", 1, now),
            ("telegram", "@disabled-channel", "Disabled", 0, now),  # 应被跳过
            ("rss", "https://www.solidot.org/index.rss", "Solidot", 1, now),
            ("unknownkind", "weird", "Unknown", 1, now),  # 未知 kind
        ])
    conn.executemany(
        "INSERT INTO topics (name, monitor_direction, enabled, created_at, updated_at) "
        "VALUES (?, ?, 1, ?, ?)",
        [
            ("GFW-政策动态", "关注 GFW 升级", now, now),
            ("协议封锁", "关注协议层", now, now),
        ])
    conn.executemany(
        "INSERT INTO topic_keywords (topic_id, keyword) VALUES (?, ?)",
        [(1, "GFW"), (1, "封锁"), (2, "VLESS"), (2, "Reality")])
    conn.executemany(
        "INSERT INTO topic_sources (topic_id, source_id, skip_keyword_filter) "
        "VALUES (?, ?, ?)",
        [(1, 1, 0), (1, 3, 0), (2, 1, 0)])  # source id 2 (disabled) 不会出现
    conn.commit()
    conn.close()
    return p


# ============== migrate dry-run ==============


async def test_migrate_dry_run_does_not_write(v1_db, tmp_db):
    r = await migrate(v1_db, tmp_db, dry_run=True)
    assert r.dry_run is True
    assert r.sources_added == 2          # telegram + rss（unknown skipped, disabled 不在 v1 sources WHERE enabled=1）
    assert r.sources_skipped_unknown_kind == 1
    assert r.topics_added == 2
    assert r.keywords_added == 4
    assert r.links_added == 3
    # 验证 dry-run 没真写
    sources = await tmp_db.list_all_sources()
    assert len(sources) == 0


# ============== migrate apply ==============


async def test_migrate_apply_writes_to_v2(v1_db, tmp_db, monkeypatch, tmp_path):
    # 隔离 KB 写入到 tmp_path 避免污染真实 KB
    monkeypatch.setattr("sentinel.kb.TOPIC_DIR", tmp_path / "03-主题")
    r = await migrate(v1_db, tmp_db, dry_run=False)
    assert r.dry_run is False
    assert r.sources_added == 2
    assert r.topics_added == 2
    assert r.keywords_added == 4
    assert r.links_added == 3
    # KB stub 应给两个 topic 都生成
    assert r.kb_stubs_created == 2
    stub_p = tmp_path / "03-主题" / "GFW-政策动态" / "信源.md"
    assert stub_p.exists()
    text = stub_p.read_text(encoding="utf-8")
    assert "GFW-政策动态" in text
    assert "industry: VPN" in text
    assert "## 信源清单" in text

    # 验证 sources
    sources = await tmp_db.list_all_sources()
    assert len(sources) == 2
    by_id = {(s["kind"], s["identifier"]) for s in sources}
    assert ("telegram", "@example_channel") in by_id
    assert ("rss", "https://www.solidot.org/index.rss") in by_id

    # 验证 topics + industry/alert_enabled 默认值
    topics = await tmp_db.list_all_topics()
    assert len(topics) == 2
    for t in topics:
        assert t["industry"] == "VPN"
        assert t["alert_enabled"] == 1

    # 验证 keywords
    for t in topics:
        kws = await tmp_db.list_topic_keywords(t["id"])
        if t["name"] == "GFW-政策动态":
            assert set(kws) == {"GFW", "封锁"}
        elif t["name"] == "协议封锁":
            assert set(kws) == {"VLESS", "Reality"}

    # 验证 topic_sources 关联
    for t in topics:
        linked = await tmp_db.list_topic_sources(t["id"])
        if t["name"] == "GFW-政策动态":
            # gfwknocker + solidot
            assert len(linked) == 2
        elif t["name"] == "协议封锁":
            assert len(linked) == 1


# ============== 幂等：重跑应该全跳过 ==============


async def test_migrate_idempotent_second_run(v1_db, tmp_db, monkeypatch,
                                            tmp_path):
    monkeypatch.setattr("sentinel.kb.TOPIC_DIR", tmp_path / "03-主题")
    # 第一次 apply
    await migrate(v1_db, tmp_db, dry_run=False)

    # 第二次 apply
    r2 = await migrate(v1_db, tmp_db, dry_run=False)
    assert r2.sources_added == 0
    assert r2.sources_skipped_exists == 2
    assert r2.topics_added == 0
    assert r2.topics_skipped_exists == 2
    # keywords 也是去重（add_topic_keyword 内部去重）
    # 但 add_topic_keyword 的"已存在跳过"内部静默，不计入 report
    # links 也是 INSERT OR REPLACE
    sources = await tmp_db.list_all_sources()
    assert len(sources) == 2  # 还是 2


# ============== format_report ==============


def test_format_report_dry_run_shows_hint():
    from sentinel.migrate import MigrateReport
    r = MigrateReport(sources_added=5, topics_added=2, dry_run=True)
    out = format_report(r)
    assert "DRY-RUN" in out
    assert "+ 5 新加" in out
    assert "+ 2 新加" in out
    assert "--apply" in out


def test_format_report_apply_no_hint():
    from sentinel.migrate import MigrateReport
    r = MigrateReport(sources_added=5, topics_added=2, dry_run=False)
    out = format_report(r)
    assert "APPLIED" in out
    assert "dry-run" not in out
