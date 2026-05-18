"""alert service · end-to-end 测试（mock LLM + mock push）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from sentinel.config import Config
from sentinel.services.alert import (
    _compress_messages, _filter_by_keywords, run_alert_service,
)
from sentinel.triage import TriageVerdict


# ===== unit tests for helpers =====


def test_filter_by_keywords_substring_case_insensitive():
    msgs = [
        {"id": 1, "source_id": 1, "content": "VLESS 协议被封"},
        {"id": 2, "source_id": 1, "content": "我饿了"},
        {"id": 3, "source_id": 1, "content": "vless test"},
    ]
    out = _filter_by_keywords(msgs, ["VLESS"], skip_filter_source_ids=set())
    assert {m["id"] for m in out} == {1, 3}


def test_filter_by_keywords_skip_filter_source_passes_all():
    msgs = [
        {"id": 1, "source_id": 1, "content": "无关内容"},
        {"id": 2, "source_id": 2, "content": "无关内容 2"},
    ]
    out = _filter_by_keywords(msgs, ["不存在"], skip_filter_source_ids={1})
    assert [m["id"] for m in out] == [1]  # source 1 直通


def test_filter_by_keywords_no_keywords_keeps_all():
    msgs = [{"id": 1, "source_id": 1, "content": "x"}]
    assert _filter_by_keywords(msgs, [], skip_filter_source_ids=set()) == msgs


def test_compress_messages_under_limit():
    msgs = [{"id": i, "source_id": 1, "posted_at": f"2026-05-1{i}"}
            for i in range(3)]
    assert _compress_messages(msgs, max_n=10) == msgs


def test_compress_messages_round_robin_over_limit():
    msgs = []
    for sid in (1, 2):
        for i in range(10):
            msgs.append({"id": sid * 100 + i, "source_id": sid,
                         "posted_at": f"2026-05-{15-i:02d}"})
    out = _compress_messages(msgs, max_n=6)
    assert len(out) <= 6
    # 至少应该有两个 source 各分到一些
    src_set = {m["source_id"] for m in out}
    assert len(src_set) == 2


# ===== service e2e =====


async def test_alert_service_no_topics_returns_success(tmp_db):
    config = Config()
    result = await run_alert_service(config, tmp_db, force=True)
    assert result["status"] == "success"
    assert result["topics_checked"] == 0
    assert result["alerts_triggered"] == 0


async def test_alert_service_skips_topic_without_sources(tmp_db):
    await tmp_db.insert_topic("Lonely", "test", "no sources linked")
    config = Config()
    result = await run_alert_service(config, tmp_db, force=True)
    assert result["status"] == "success"
    assert result["topics_checked"] == 1
    assert result["alerts_triggered"] == 0


async def test_alert_service_writes_alert_when_llm_says_yes(tmp_db,
                                                            monkeypatch,
                                                            tmp_path):
    # 建 source + topic + 关联 + 关键词
    sid = await tmp_db.insert_source("rss", "https://example.com/rss",
                                     "Test Feed")
    tid = await tmp_db.insert_topic("TestTopic", "test", "monitor x")
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.add_topic_keyword(tid, "事件")

    # 插一条命中关键词的 message
    await tmp_db.insert_message(
        sid, "ext-1", "alice", "今天发生大事件 影响很大",
        "https://example.com/1",
        datetime.now(timezone.utc),
    )

    # mock LLM 返回 worth_alert=true
    fake_response = (
        '{"worth_alert": true, "headline": "测试告警",'
        ' "summary": "短描述", "related_message_ids": [1]}'
    )
    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda prompt, *, model, cli_path, timeout: fake_response,
    )
    # mock KB archive root 到 tmp_path（避免污染真 KB）
    monkeypatch.setattr("sentinel.kb.ALERT_ARCHIVE_DIR", tmp_path)

    config = Config()  # 无 TG creds → push 失败但不影响 alert 写入
    result = await run_alert_service(config, tmp_db, force=True)

    assert result["alerts_triggered"] == 1
    # 验证 alerts 表
    recent = await tmp_db.recent_alerts(limit=5)
    assert len(recent) == 1
    assert recent[0]["headline"] == "测试告警"
    assert recent[0]["push_status"] == "failed"  # 无 creds，push 失败


async def test_alert_service_24h_dedup(tmp_db, monkeypatch, tmp_path):
    """同 headline 24h 内只算一次。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("T", "test", "x")
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.add_topic_keyword(tid, "事件")
    await tmp_db.insert_message(
        sid, "ext-1", "u", "事件来了", None,
        datetime.now(timezone.utc))

    fake = '{"worth_alert": true, "headline": "X", "summary": "y", "related_message_ids": [1]}'
    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda prompt, *, model, cli_path, timeout: fake)
    monkeypatch.setattr("sentinel.kb.ALERT_ARCHIVE_DIR", tmp_path)

    config = Config()
    # 第一次：插入 alert + push 失败 → status=failed
    # 注：alert_exists_recently 看 status='sent'，所以这里 failed 不挡 dedup
    # 我们手动 mock push 成功
    async def fake_push(*args, **kwargs):
        return True
    monkeypatch.setattr("sentinel.services.alert.send_telegram", fake_push)

    r1 = await run_alert_service(config, tmp_db, force=True)
    assert r1["alerts_triggered"] == 1

    # 第二次：同 headline 应被 dedup
    r2 = await run_alert_service(config, tmp_db, force=True)
    assert r2["alerts_triggered"] == 0


async def test_alert_service_per_topic_failure_isolated(tmp_db, monkeypatch,
                                                       tmp_path):
    """一个 topic 的 LLM 失败不能挂掉其他 topic 的判定。"""
    # 两个 topic 都 link 同一 source，各有一条命中关键词的消息
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    t1 = await tmp_db.insert_topic("BoomTopic", "test", "x")
    t2 = await tmp_db.insert_topic("OkTopic", "test", "y")
    await tmp_db.link_topic_source(t1, sid)
    await tmp_db.link_topic_source(t2, sid)
    await tmp_db.add_topic_keyword(t1, "事件")
    await tmp_db.add_topic_keyword(t2, "事件")
    await tmp_db.insert_message(
        sid, "ext-1", "u", "事件来了", None,
        datetime.now(timezone.utc))

    fake_ok = ('{"worth_alert": true, "headline": "正常 headline",'
               ' "summary": "y", "related_message_ids": [1]}')

    # t1 触发 LLM 异常；t2 正常返回
    def selective_cli(prompt, *, model, cli_path, timeout):
        if "BoomTopic" in prompt:
            raise RuntimeError("simulated LLM failure")
        return fake_ok

    monkeypatch.setattr("sentinel.triage._call_cli", selective_cli)
    monkeypatch.setattr("sentinel.kb.ALERT_ARCHIVE_DIR", tmp_path)

    async def fake_push(*args, **kwargs):
        return True
    monkeypatch.setattr("sentinel.services.alert.send_telegram", fake_push)

    config = Config()
    result = await run_alert_service(config, tmp_db, force=True)

    # t2 应该成功告警；t1 错误被记到 topic_errors
    assert result["alerts_triggered"] == 1
    assert result["status"] == "partial"
    assert len(result["topic_errors"]) == 1
    assert "BoomTopic" in result["topic_errors"][0]
    # service_run 整体仍然 status=success（partial 不挂 run），但 error 字段含 topic_errors
    import aiosqlite
    async with aiosqlite.connect(tmp_db.db_path) as raw:
        raw.row_factory = aiosqlite.Row
        async with raw.execute(
            "SELECT status, error FROM service_runs WHERE service='alert' "
            "ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
        assert row["status"] == "success"
        assert "topic_errors" in (row["error"] or "")


async def test_alert_service_archive_failure_marks_partial(tmp_db, monkeypatch,
                                                          tmp_path):
    """KB 归档异常应被记入 archive_errors + service_run.error (partial)，但 alert 仍 push 成功。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("T", "test", "x")
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.add_topic_keyword(tid, "事件")
    await tmp_db.insert_message(
        sid, "ext-1", "u", "事件来了", None,
        datetime.now(timezone.utc))

    fake = '{"worth_alert": true, "headline": "X", "summary": "y", "related_message_ids": [1]}'
    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda prompt, *, model, cli_path, timeout: fake)

    # 强制归档失败
    def boom_archive(*args, **kwargs):
        raise OSError("simulated disk failure")
    monkeypatch.setattr("sentinel.services.alert.append_alert_archive",
                       boom_archive)

    async def fake_push(*args, **kwargs):
        return True
    monkeypatch.setattr("sentinel.services.alert.send_telegram", fake_push)

    config = Config()
    result = await run_alert_service(config, tmp_db, force=True)
    assert result["alerts_triggered"] == 1
    assert result["status"] == "partial"
    assert len(result["archive_errors"]) == 1
    assert "simulated disk failure" in result["archive_errors"][0]

    # service_run.error 应该含 archive_errors
    import aiosqlite
    async with aiosqlite.connect(tmp_db.db_path) as raw:
        raw.row_factory = aiosqlite.Row
        async with raw.execute(
            "SELECT status, error FROM service_runs WHERE service='alert' "
            "ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
        assert row["status"] == "success"  # run 整体 success，但 error 字段标 partial
        assert "archive_errors" in (row["error"] or "")


async def test_alert_service_backfill_consumed_once(tmp_db, monkeypatch,
                                                   tmp_path):
    """topic.backfill_hours > 0 时，第一次 alert 用 backfill 窗口扫历史消息；
    跑完 backfill_hours 应被清零，第二次 alert 回到正常 since 逻辑。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    # 用 backfill=72h 创建 topic
    tid = await tmp_db.insert_topic("FreshTopic", "test", "x",
                                    backfill_hours=72)
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.add_topic_keyword(tid, "事件")
    # 30h 前的存量消息（落在 backfill 72h 窗口内）
    old_when = datetime.now(timezone.utc) - timedelta(hours=30)
    await tmp_db.insert_message(sid, "ext-old", "u", "事件来了", None, old_when)

    fake = '{"worth_alert": true, "headline": "X", "summary": "y", "related_message_ids": [1]}'
    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda prompt, *, model, cli_path, timeout: fake)
    monkeypatch.setattr("sentinel.kb.ALERT_ARCHIVE_DIR", tmp_path)

    async def fake_push(*args, **kwargs):
        return True
    monkeypatch.setattr("sentinel.services.alert.send_telegram", fake_push)

    config = Config()
    r1 = await run_alert_service(config, tmp_db, force=True)
    assert r1["alerts_triggered"] == 1  # backfill 让 30h 前消息可见

    # backfill_hours 应被清零
    topic_after = await tmp_db.get_topic(tid)
    assert topic_after["backfill_hours"] == 0

    # 再插一条 26h 前的消息，第二次跑 since=上次 run 起点（数秒前），
    # 26h 前的消息应在新窗口外
    await tmp_db.insert_message(
        sid, "ext-26h", "u", "事件继续", None,
        datetime.now(timezone.utc) - timedelta(hours=26))
    # mock 不同 headline 避免 24h dedup
    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda prompt, *, model, cli_path, timeout:
            '{"worth_alert": true, "headline": "Y", "summary": "z", "related_message_ids": [2]}')
    r2 = await run_alert_service(config, tmp_db, force=True)
    # 26h 前的消息在 alert.since 范围外（since 是 last_run - 5min），不应触发
    assert r2["alerts_triggered"] == 0


async def test_alert_service_dedup_fuzzy_match(tmp_db, monkeypatch, tmp_path):
    """归一化 dedup 应抓"X 升级" vs "X 贸易 升级" 这类同义改写。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("T", "test", "x")
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.add_topic_keyword(tid, "事件")
    await tmp_db.insert_message(sid, "ext-1", "u", "事件来了", None,
                                datetime.now(timezone.utc))
    monkeypatch.setattr("sentinel.kb.ALERT_ARCHIVE_DIR", tmp_path)

    async def fake_push(*args, **kwargs):
        return True
    monkeypatch.setattr("sentinel.services.alert.send_telegram", fake_push)

    # 第一次：headline = "中美关税升级风险"
    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda *a, **kw:
            '{"worth_alert": true, "headline": "中美关税升级风险",'
            ' "summary": "y", "related_message_ids": [1]}')
    config = Config()
    r1 = await run_alert_service(config, tmp_db, force=True)
    assert r1["alerts_triggered"] == 1

    # 第二次：headline = "中美贸易关税升级风险加大"（同义改写）→ 应被 fuzzy dedup
    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda *a, **kw:
            '{"worth_alert": true, "headline": "中美贸易关税升级风险加大",'
            ' "summary": "z", "related_message_ids": [1]}')
    r2 = await run_alert_service(config, tmp_db, force=True)
    assert r2["alerts_triggered"] == 0  # 模糊匹配命中


async def test_alert_service_dedup_allows_different_event(tmp_db, monkeypatch,
                                                         tmp_path):
    """完全不同的事件不应被误 dedup（避免假阴性）。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("T2", "test", "x")
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.add_topic_keyword(tid, "事件")
    await tmp_db.insert_message(sid, "ext-1", "u", "事件来了", None,
                                datetime.now(timezone.utc))
    monkeypatch.setattr("sentinel.kb.ALERT_ARCHIVE_DIR", tmp_path)

    async def fake_push(*args, **kwargs):
        return True
    monkeypatch.setattr("sentinel.services.alert.send_telegram", fake_push)

    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda *a, **kw:
            '{"worth_alert": true, "headline": "印度大选结果出炉",'
            ' "summary": "y", "related_message_ids": [1]}')
    r1 = await run_alert_service(Config(), tmp_db, force=True)
    assert r1["alerts_triggered"] == 1

    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda *a, **kw:
            '{"worth_alert": true, "headline": "日本央行加息预期升温",'
            ' "summary": "z", "related_message_ids": [1]}')
    r2 = await run_alert_service(Config(), tmp_db, force=True)
    assert r2["alerts_triggered"] == 1  # 完全不同的事件，不应 dedup


async def test_alert_freq_gate_skips_when_not_enough_time(tmp_db, monkeypatch,
                                                        tmp_path):
    """topic.alert_interval_hours=12 + last_checked 5h 前 → skip 不进 LLM。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("Freq", "test", "x",
                                   alert_interval_hours=12)
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.add_topic_keyword(tid, "事件")
    await tmp_db.insert_message(sid, "ext-1", "u", "事件来了", None,
                                datetime.now(timezone.utc))

    # last_checked 5h 前（< 12h 间隔）
    five_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    import aiosqlite
    async with aiosqlite.connect(tmp_db.db_path) as raw:
        await raw.execute(
            "UPDATE topics SET last_alert_checked_at=? WHERE id=?",
            (five_hours_ago, tid))
        await raw.commit()

    # LLM 设成"如果调用就抛"——验证完全没进 LLM
    def llm_should_not_be_called(*a, **kw):
        raise AssertionError("LLM should not be called when freq gate skips!")
    monkeypatch.setattr("sentinel.triage._call_cli", llm_should_not_be_called)

    config = Config()
    # NOTE: 不 force, freq gate 才生效
    result = await run_alert_service(config, tmp_db, force=False)
    assert result["topics_skipped_freq"] == 1
    assert result["topics_processed"] == 0


async def test_alert_freq_gate_runs_when_interval_elapsed(tmp_db, monkeypatch,
                                                        tmp_path):
    """interval=12h, last_checked=15h 前 → 应该跑。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("Freq2", "test", "x",
                                   alert_interval_hours=12)
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.add_topic_keyword(tid, "事件")
    await tmp_db.insert_message(sid, "ext-1", "u", "事件来了", None,
                                datetime.now(timezone.utc))

    fifteen_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=15)).isoformat()
    import aiosqlite
    async with aiosqlite.connect(tmp_db.db_path) as raw:
        await raw.execute(
            "UPDATE topics SET last_alert_checked_at=? WHERE id=?",
            (fifteen_hours_ago, tid))
        await raw.commit()

    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda prompt, *, model, cli_path, timeout:
            '{"worth_alert": false, "headline": "x", "summary": "y", "related_message_ids": []}')
    monkeypatch.setattr("sentinel.kb.ALERT_ARCHIVE_DIR", tmp_path)

    config = Config()
    result = await run_alert_service(config, tmp_db, force=False)
    assert result["topics_skipped_freq"] == 0
    assert result["topics_processed"] == 1
    # 跑完后 last_alert_checked_at 应更新到接近 now
    topic_after = await tmp_db.get_topic(tid)
    async with aiosqlite.connect(tmp_db.db_path) as raw:
        raw.row_factory = aiosqlite.Row
        async with raw.execute(
            "SELECT last_alert_checked_at FROM topics WHERE id=?", (tid,)) as cur:
            row = await cur.fetchone()
        new_lc = datetime.fromisoformat(row["last_alert_checked_at"])
    age = (datetime.now(timezone.utc) - new_lc).total_seconds()
    assert age < 10  # 几秒内


async def test_alert_freq_gate_interval_zero_uses_global(tmp_db, monkeypatch,
                                                       tmp_path):
    """alert_interval_hours=0 → 不走 freq gate (跟全局 alert 频率)。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("Default", "test", "x",
                                   alert_interval_hours=0)
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.add_topic_keyword(tid, "事件")
    await tmp_db.insert_message(sid, "ext-1", "u", "事件来了", None,
                                datetime.now(timezone.utc))

    # 即使 last_checked 1 分钟前，interval=0 也应该跑
    one_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    import aiosqlite
    async with aiosqlite.connect(tmp_db.db_path) as raw:
        await raw.execute(
            "UPDATE topics SET last_alert_checked_at=? WHERE id=?",
            (one_min_ago, tid))
        await raw.commit()

    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda *a, **kw:
            '{"worth_alert": false, "headline": "x", "summary": "y", "related_message_ids": []}')
    monkeypatch.setattr("sentinel.kb.ALERT_ARCHIVE_DIR", tmp_path)

    config = Config()
    result = await run_alert_service(config, tmp_db, force=False)
    assert result["topics_skipped_freq"] == 0
    assert result["topics_processed"] == 1


async def test_alert_force_bypasses_freq_gate(tmp_db, monkeypatch, tmp_path):
    """--force 应该绕过 freq gate (人工触发就该跑)。"""
    sid = await tmp_db.insert_source("rss", "https://x/rss")
    tid = await tmp_db.insert_topic("FreqF", "test", "x",
                                   alert_interval_hours=24)
    await tmp_db.link_topic_source(tid, sid)
    await tmp_db.add_topic_keyword(tid, "事件")
    await tmp_db.insert_message(sid, "ext-1", "u", "事件来了", None,
                                datetime.now(timezone.utc))

    one_h_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    import aiosqlite
    async with aiosqlite.connect(tmp_db.db_path) as raw:
        await raw.execute(
            "UPDATE topics SET last_alert_checked_at=? WHERE id=?",
            (one_h_ago, tid))
        await raw.commit()

    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda *a, **kw:
            '{"worth_alert": false, "headline": "x", "summary": "y", "related_message_ids": []}')
    monkeypatch.setattr("sentinel.kb.ALERT_ARCHIVE_DIR", tmp_path)

    config = Config()
    # force=True 应该绕过 24h gate
    result = await run_alert_service(config, tmp_db, force=True)
    assert result["topics_processed"] == 1
