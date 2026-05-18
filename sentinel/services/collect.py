"""Collect service · 调度 collectors，把 messages 入库。

业务核心，不调 LLM。运维外壳由 sentinel.runtime.shell.ServiceShell 包装。

主流程（参见 ~/.kb/.../设计.md §3.2）：
  1. SELECT enabled sources
  2. 遍历 source → invoke collector → 写 messages
  3. 单源失败隔离（增加 failure_count，达上限 auto-disable）
  4. 跑完 purge_old_messages(retention_days)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sentinel.collectors import KIND_REGISTRY, auto_discover
from sentinel.config import Config
from sentinel.db import Database
from sentinel.runtime.shell import ServiceShell


log = logging.getLogger(__name__)


async def run_collect_service(config: Config, db: Database,
                              force: bool = False) -> dict:
    """跑一次采集。Returns 统计 dict。"""
    auto_discover()

    async with ServiceShell("collect", config, db, force=force) as shell:
        if not shell.should_run:
            log.info("collect skipped: %s", shell.state.skip_reason)
            return {"status": "skipped", "reason": shell.state.skip_reason}

        lookback_h = shell.state.lookback_hours or 48.0
        since = datetime.now(timezone.utc) - timedelta(hours=lookback_h)
        log.info("collect run: since=%s (lookback=%.1fh)",
                 since.isoformat(), lookback_h)

        sources = await db.list_enabled_sources()
        log.info("enabled sources: %d", len(sources))

        total_collected = 0
        total_skipped_dup = 0
        failed_sources = 0
        failure_limit = config.services.collect.source_failure_limit

        for source in sources:
            kind = source["kind"]
            if kind not in KIND_REGISTRY:
                log.warning("unknown source kind: %s (id=%s)", kind, source["id"])
                continue

            try:
                collector_cls = KIND_REGISTRY[kind]
                # collector 拿到 config dict（含 secrets）
                cfg_dict = config.model_dump()
                collector = collector_cls(source_row=source, config=cfg_dict)

                src_collected = 0
                async for msg in collector.collect(since=since):
                    inserted_id = await db.insert_message(
                        source_id=source["id"],
                        external_id=msg.external_id,
                        author=msg.author,
                        content=msg.content,
                        url=msg.url,
                        posted_at=msg.posted_at,
                        raw_json=msg.raw_json,
                    )
                    if inserted_id is not None:
                        src_collected += 1
                    else:
                        total_skipped_dup += 1
                total_collected += src_collected
                log.info("source #%s [%s] %s: +%d messages",
                         source["id"], kind,
                         source.get("display_name") or source["identifier"],
                         src_collected)
                # 成功 → 重置失败计数
                if source.get("failure_count", 0) > 0:
                    await db.reset_failure(source["id"])

            except Exception as e:
                failed_sources += 1
                count = await db.increment_failure(source["id"])
                log.error("source #%s [%s] failed (%d/%d): %s",
                          source["id"], kind, count, failure_limit, e)
                if count >= failure_limit:
                    await db.disable_source(source["id"])
                    log.warning("source #%s auto-disabled after %d failures",
                                source["id"], count)
                # 不抛异常，单源失败隔离

        # retention 清理（messages + service_runs；alerts 永不清）
        purged_msgs = await db.purge_old_messages(
            config.services.collect.retention_days)
        if purged_msgs:
            log.info("purged %d old messages (retention=%dd)",
                     purged_msgs, config.services.collect.retention_days)
        purged_runs = await db.purge_old_service_runs(
            config.services.collect.service_runs_retention_days)
        if purged_runs:
            log.info("purged %d old service_runs (retention=%dd)",
                     purged_runs,
                     config.services.collect.service_runs_retention_days)

        # 写回统计
        shell.record(messages_collected=total_collected)
        return {
            "status": "success",
            "messages_collected": total_collected,
            "skipped_duplicates": total_skipped_dup,
            "failed_sources": failed_sources,
            "purged_old": purged_msgs,
            "purged_runs": purged_runs,
        }
