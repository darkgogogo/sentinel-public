"""advisor service · 信噪比计算 + section 更新 + e2e mock LLM。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from sentinel.config import Config
from sentinel.kb import update_advisor_section, ensure_topic_sources_file
from sentinel.services.advisor import (
    AdvisorResult, SourceStats, _build_prompt,
    _collect_stats, _format_section_md, run_advisor_service,
)


# ============== 数据聚合 ==============


async def test_collect_stats_basic_signal_ratio(tmp_db):
    """3 source 不同命中率，验证 signal_ratio。"""
    s1 = await tmp_db.insert_source("rss", "url-1", "src-1")
    s2 = await tmp_db.insert_source("rss", "url-2", "src-2")
    s3 = await tmp_db.insert_source("rss", "url-3", "src-3")
    tid = await tmp_db.insert_topic("T", "g", "x")

    now = datetime.now(timezone.utc)
    # 插 messages: s1 10 条，s2 5 条，s3 0 条
    for i in range(10):
        await tmp_db.insert_message(s1, f"e1-{i}", None, "x", None, now)
    for i in range(5):
        await tmp_db.insert_message(s2, f"e2-{i}", None, "y", None, now)

    # alert 引用 s1 的 3 条 + s2 的 1 条
    # 拿出真实 message id
    import aiosqlite
    async with aiosqlite.connect(tmp_db.db_path) as raw:
        raw.row_factory = aiosqlite.Row
        async with raw.execute(
            "SELECT id FROM messages WHERE source_id=? ORDER BY id LIMIT 3",
            (s1,)) as cur:
            s1_ids = [r[0] for r in await cur.fetchall()]
        async with raw.execute(
            "SELECT id FROM messages WHERE source_id=? ORDER BY id LIMIT 1",
            (s2,)) as cur:
            s2_ids = [r[0] for r in await cur.fetchall()]
    await tmp_db.insert_alert(tid, "h", "s", s1_ids + s2_ids,
                              push_status="sent")

    sources = [
        {"id": s1, "kind": "rss", "identifier": "url-1",
         "display_name": "src-1", "failure_count": 0, "enabled": 1},
        {"id": s2, "kind": "rss", "identifier": "url-2",
         "display_name": "src-2", "failure_count": 0, "enabled": 1},
        {"id": s3, "kind": "rss", "identifier": "url-3",
         "display_name": "src-3", "failure_count": 2, "enabled": 1},
    ]
    stats, alerts = await _collect_stats(
        topic={"id": tid, "name": "T"},
        db=tmp_db,
        since=now - timedelta(days=30),
        sources=sources,
    )
    by_id = {s.source_id: s for s in stats}
    assert by_id[s1].total_messages == 10
    assert by_id[s1].alerted_messages == 3
    assert abs(by_id[s1].signal_ratio - 0.3) < 0.01

    assert by_id[s2].total_messages == 5
    assert by_id[s2].alerted_messages == 1
    assert abs(by_id[s2].signal_ratio - 0.2) < 0.01

    assert by_id[s3].total_messages == 0
    assert by_id[s3].signal_ratio == 0.0

    assert len(alerts) == 1


# ============== prompt ==============


def test_build_prompt_uses_industry_and_stats():
    stats = [SourceStats(
        source_id=1, kind="rss", identifier="u", display_name="X",
        total_messages=10, alerted_messages=3, failure_count=0,
    )]
    result = AdvisorResult(
        topic={"name": "T", "industry": "VPN",
               "monitor_direction": "x"},
        window_days=30, source_stats=stats,
        total_alerts=2, true_positive_count=1, false_positive_count=1,
        unlabeled_count=0,
    )
    prompt = _build_prompt(result.topic, 30, result)
    assert "VPN" in prompt
    assert "[rss] X" in prompt
    assert "总消息 10" in prompt
    assert "信噪比 0.3000" in prompt


# ============== section markdown 格式 ==============


def test_format_section_includes_table_and_recommendations():
    result = AdvisorResult(
        topic={"name": "T", "industry": "x"},
        window_days=30,
        source_stats=[
            SourceStats(1, "rss", "u", "X",
                        total_messages=10, alerted_messages=3),
            SourceStats(2, "rss", "u2", "Y",
                        total_messages=2, alerted_messages=0,
                        failure_count=5, enabled=False),
        ],
        total_alerts=3, true_positive_count=2,
        false_positive_count=1, unlabeled_count=0,
        llm_recommendations="- 建议关闭：Y · 信噪比 0\n- 建议加权：X",
    )
    md = _format_section_md(result)
    assert "## advisor 建议" in md
    assert "窗口 30 天" in md
    assert "⭐ 准 2" in md
    assert "🚫 误报 1" in md
    assert "| [rss] X | 10 | 3 | 0.3000 |" in md
    assert "**disabled**" in md  # 禁用 source 标记
    assert "建议关闭：Y" in md
    assert "建议加权：X" in md


# ============== KB section 更新 ==============


def test_update_advisor_section_preserves_other_sections(tmp_path):
    topic = {"name": "T", "industry": "g", "monitor_direction": "x",
             "alert_enabled": 1}
    sources = [{"kind": "rss", "identifier": "u", "display_name": "X"}]
    p = ensure_topic_sources_file(topic, sources, topic_root=tmp_path)

    # 用户在 信源清单 段后加自定义 section
    text = p.read_text()
    text = text.replace(
        "## advisor 建议",
        "## 用户自定义段\n\n这是用户自己加的内容，不能被覆盖。\n\n## advisor 建议")
    p.write_text(text)

    new_section = (
        "## advisor 建议\n\n> 最近更新: 2026-05-15\n\n"
        "- 建议关闭 src-X\n\n"
        "### 信源数据\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    update_advisor_section(topic, sources, new_section, topic_root=tmp_path)

    content = p.read_text()
    # 用户自定义段保留
    assert "## 用户自定义段" in content
    assert "用户自己加的内容" in content
    # 新 advisor section 替换了旧的
    assert "建议关闭 src-X" in content
    # 旧 advisor section 占位词 "暂无 · advisor service" 应该被替换掉
    assert "暂无 · advisor service" not in content


def test_update_advisor_section_handles_missing_section(tmp_path):
    """信源.md 不存在 advisor 段时应追加。"""
    topic = {"name": "T", "industry": "g", "monitor_direction": "x"}
    sources = [{"kind": "rss", "identifier": "u", "display_name": "X"}]
    p = topic_root = tmp_path
    # 手动建一个无 advisor 段的文件
    target = tmp_path / "T" / "信源.md"
    target.parent.mkdir()
    target.write_text("# T\n\n## 元信息\nx\n")

    section = "## advisor 建议\n\n建议 X\n"
    update_advisor_section(topic, sources, section, topic_root=topic_root)

    # 注：ensure_topic_sources_file 检查的是 topic_slug 路径，必须是 topic.name 路径
    # 当文件已存在（即使无 advisor 段）也不重建
    new_content = target.read_text()
    assert "建议 X" in new_content
    assert "## 元信息" in new_content  # 原有段保留


# ============== e2e ==============


async def test_run_advisor_service_topic_not_found(tmp_db):
    config = Config()
    result = await run_advisor_service(
        config, tmp_db, topic_name="不存在", window_days=30)
    assert result["status"] == "failed"
    assert "topic 不存在" in result["error"]


async def test_run_advisor_service_e2e_with_mock_llm(
    tmp_db, monkeypatch, tmp_path,
):
    sid = await tmp_db.insert_source("rss", "https://x/rss", "Test Feed")
    tid = await tmp_db.insert_topic("T", "test", "x")
    await tmp_db.link_topic_source(tid, sid)
    now = datetime.now(timezone.utc)
    await tmp_db.insert_message(sid, "ext-1", None, "hello", None, now)

    monkeypatch.setattr(
        "sentinel.services.advisor._call_cli",
        lambda prompt, *, model, cli_path, timeout:
            "- 建议关闭 Test Feed\n- 推荐关键词：x",
    )
    # 重定向 KB 写入到 tmp_path
    monkeypatch.setattr("sentinel.kb.TOPIC_DIR", tmp_path)

    config = Config()
    result = await run_advisor_service(
        config, tmp_db, topic_name="T", window_days=30)
    assert result["status"] == "success"
    assert result["llm_ok"] is True
    assert result["source_count"] == 1
    # 验证 KB 文件被写入
    from sentinel.kb import topic_slug
    kb_path = tmp_path / topic_slug("T") / "信源.md"
    assert kb_path.exists()
    content = kb_path.read_text()
    assert "建议关闭 Test Feed" in content
    assert "## advisor 建议" in content
