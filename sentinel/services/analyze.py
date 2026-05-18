"""analyze service · 人工触发深度报告。

设计区别于 collect/alert：
- 人工触发，不走 launchd 定时
- 不查 12h 阈值（无 threshold_hours），但仍记 service_runs（status/messages_evaluated 等）
- 输入参数：topic_name + period_hours + mode（auto/single/timeseries）
- 输出：markdown 写 KB + 返回 dict 含 file_path
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sentinel.analyze.data import build_analysis
from sentinel.analyze.render import write_report_to_kb
from sentinel.config import Config
from sentinel.db import Database
from sentinel.services.alert import _filter_by_keywords, _compress_messages


log = logging.getLogger(__name__)


async def run_analyze_service(config: Config, db: Database, *,
                             topic_name: str,
                             period_hours: int = 168,
                             mode: str = "auto") -> dict:
    """跑一次深度分析报告。

    不走 ServiceShell（人工触发，无阈值守门），但记 service_runs 以备追溯。
    """
    # 找 topic
    topics = await db.list_all_topics()
    topic = next((t for t in topics if t["name"] == topic_name), None)
    if not topic:
        msg = f"topic 不存在: {topic_name!r}"
        log.error(msg)
        return {"status": "failed", "error": msg}

    run_id = await db.insert_run(
        service="analyze", lookback_hours=float(period_hours),
        status="running")

    try:
        keywords = await db.list_topic_keywords(topic["id"])
        sources = await db.list_topic_sources(topic["id"])
        if not sources:
            await db.update_run(run_id, status="failed",
                                error="topic 无关联 source")
            return {"status": "failed", "error": "topic 无关联 source"}

        source_ids = [s["id"] for s in sources]
        skip_filter_ids = {s["id"] for s in sources
                          if s.get("skip_keyword_filter")}

        since = datetime.now(timezone.utc) - timedelta(hours=period_hours)
        raw_msgs = await db.list_messages_since(source_ids, since)
        matched = _filter_by_keywords(raw_msgs, keywords, skip_filter_ids)

        # 限制最大消息数（cluster prompt 输入太大会失败）
        # analyze 用更大的限额（cluster 比 triage 能消化更多）
        MAX_MESSAGES_FOR_LLM = 800
        if len(matched) > MAX_MESSAGES_FOR_LLM:
            log.info("messages %d > %d, 压缩",
                     len(matched), MAX_MESSAGES_FOR_LLM)
            matched = _compress_messages(matched, MAX_MESSAGES_FOR_LLM)

        log.info("analyze: topic=%s period=%dh raw=%d matched=%d mode=%s",
                 topic_name, period_hours, len(raw_msgs), len(matched), mode)

        data = await build_analysis(
            topic=topic, messages=matched,
            period_hours=period_hours, mode=mode, config=config)

        path = write_report_to_kb(data)

        await db.update_run(
            run_id, status="success",
            messages_collected=len(matched),  # 复用字段（messages_evaluated 语义）
        )

        return {
            "status": "success",
            "file_path": str(path),
            "headline": data.headline,
            "mode_used": data.mode_used,
            "message_count": data.message_count,
            "source_count": data.source_count,
            "issues_count": len(data.issues),
            "errors": data.errors,
        }
    except Exception as e:
        log.exception("analyze 失败")
        await db.update_run(run_id, status="failed", error=str(e))
        return {"status": "failed", "error": str(e)}
