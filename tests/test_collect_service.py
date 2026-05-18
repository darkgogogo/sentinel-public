"""collect service end-to-end · 用 mock collector 模拟外界源。

验证：
- collect service 写消息进 SQLite
- 单源失败不影响其他源
- 失败 source N 次后 auto-disable
- duplicate external_id 不重复入库
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

import pytest

from sentinel.collectors import (
    KIND_REGISTRY, register, BaseCollector, CollectedMessage,
)
from sentinel.config import Config
from sentinel.services.collect import run_collect_service


# ------ test fixtures: 注册 mock collectors ------


class _GoodMockCollector(BaseCollector):
    KIND = "mock-good"

    async def collect(self, since) -> AsyncIterator[CollectedMessage]:
        for i in range(3):
            yield CollectedMessage(
                external_id=f"good-{self.source['id']}-{i}",
                author="alice",
                content=f"good msg {i}",
                url=None,
                posted_at=datetime.now(timezone.utc),
                raw_json={},
            )

    @classmethod
    async def resolve(cls, identifier, config):
        return {"display_name": identifier}


class _FailMockCollector(BaseCollector):
    KIND = "mock-fail"

    async def collect(self, since):
        raise RuntimeError("simulated failure")
        yield  # unreachable, keep generator type

    @classmethod
    async def resolve(cls, identifier, config):
        return {"display_name": identifier}


@pytest.fixture(autouse=True)
def _register_mocks():
    register(_GoodMockCollector)
    register(_FailMockCollector)
    yield
    KIND_REGISTRY.pop("mock-good", None)
    KIND_REGISTRY.pop("mock-fail", None)


# ------ tests ------


async def test_collect_writes_messages_to_db(tmp_db):
    await tmp_db.insert_source("mock-good", "src-1")
    config = Config()
    result = await run_collect_service(config, tmp_db, force=True)
    assert result["status"] == "success"
    assert result["messages_collected"] == 3


async def test_collect_isolates_per_source_failure(tmp_db):
    """一个 source fail 不影响另一个 good source。"""
    await tmp_db.insert_source("mock-good", "src-good")
    await tmp_db.insert_source("mock-fail", "src-bad")
    config = Config()
    result = await run_collect_service(config, tmp_db, force=True)
    assert result["status"] == "success"
    assert result["messages_collected"] == 3
    assert result["failed_sources"] == 1


async def test_failure_count_increments_and_auto_disable(tmp_db):
    """连续 N 次失败 → auto disable。"""
    sid = await tmp_db.insert_source("mock-fail", "src-bad")
    config = Config()
    # 阈值默认 5
    for _ in range(5):
        await run_collect_service(config, tmp_db, force=True)

    sources = await tmp_db.list_enabled_sources()
    # 被 disable 的 source 不在 enabled 列表
    assert all(s["id"] != sid for s in sources)


async def test_duplicate_external_id_not_reinserted(tmp_db):
    """跑两次，第二次 messages_collected=0（external_id UNIQUE 触发）。"""
    await tmp_db.insert_source("mock-good", "src-1")
    config = Config()
    await run_collect_service(config, tmp_db, force=True)
    result2 = await run_collect_service(config, tmp_db, force=True)
    # 第二次：3 个 message 都重复
    assert result2["messages_collected"] == 0
    assert result2["skipped_duplicates"] == 3


async def test_collect_purges_old_service_runs(tmp_db, monkeypatch):
    """collect 跑完应清 service_runs > retention 的记录。"""
    import aiosqlite
    from sentinel.services.collect import run_collect_service
    from sentinel.config import Config

    # 插一条 400 天前的 service_run（旧）
    async with aiosqlite.connect(tmp_db.db_path) as raw:
        await raw.execute(
            "INSERT INTO service_runs (service, started_at, status) "
            "VALUES ('collect', datetime('now', '-400 days'), 'success')")
        # 一条 30 天前（新）
        await raw.execute(
            "INSERT INTO service_runs (service, started_at, status) "
            "VALUES ('collect', datetime('now', '-30 days'), 'success')")
        await raw.commit()

    config = Config()
    config.services.collect.service_runs_retention_days = 365

    result = await run_collect_service(config, tmp_db, force=True)
    assert result["status"] == "success"
    assert result["purged_runs"] == 1  # 400 天前那条被清

    async with aiosqlite.connect(tmp_db.db_path) as raw:
        async with raw.execute(
            "SELECT COUNT(*) FROM service_runs "
            "WHERE started_at < datetime('now', '-365 days')") as cur:
            row = await cur.fetchone()
        assert row[0] == 0
