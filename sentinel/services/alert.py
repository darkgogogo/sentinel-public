"""Alert service · 业务核心。

主流程（参见 ~/.kb/.../设计.md §3.3）：
  1. SELECT topics WHERE alert_enabled=1
  2. 对每个 topic：
     a. 拉关联 sources + keywords
     b. 取自上次 alert 成功 run 以来的新 messages（带回溯封顶）
     c. keyword 过滤（含 skip_keyword_filter 直通）
     d. compress 到 triage_max_messages
     e. 调 triage 判定 → TriageVerdict
     f. worth_alert=true → 24h dedup → insert alert → push + KB 归档
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sentinel.config import Config
from sentinel.db import Database
from sentinel.kb import append_alert_archive
from sentinel.push import send_telegram
from sentinel.runtime.shell import ServiceShell
from sentinel.triage import judge


log = logging.getLogger(__name__)


def _filter_by_keywords(messages: list[dict], keywords: list[str],
                       skip_filter_source_ids: set[int]) -> list[dict]:
    """keyword 过滤 · 任一关键词 substring 命中即纳入。

    source 在 topic_sources 表标 skip_keyword_filter=1 时整源直通（白名单源）。
    无 keywords 时所有消息保留（topic 早期无关键词的情况）。
    """
    if not keywords:
        return messages

    # case-insensitive substring（中文 OK，英文也命中）
    pattern = re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE)
    out = []
    for m in messages:
        if m.get("source_id") in skip_filter_source_ids:
            out.append(m)
            continue
        if pattern.search(m.get("content", "")):
            out.append(m)
    return out


def _compress_messages(messages: list[dict], max_n: int) -> list[dict]:
    """超过 max_n 时做源轮转 + 最近优先截断 · 防 LLM 单次输入太长。"""
    if len(messages) <= max_n:
        return messages
    # 简单实现：按源 round-robin 取，每源最多 N/sources 条
    by_source: dict[int, list[dict]] = {}
    for m in messages:
        by_source.setdefault(m["source_id"], []).append(m)
    # 每个 source 已按 posted_at DESC 排（db.list_messages_since），取头部
    per_source = max(1, max_n // max(1, len(by_source)))
    compressed: list[dict] = []
    for sid, ms in by_source.items():
        compressed.extend(ms[:per_source])
    # 再截断到 max_n
    compressed.sort(key=lambda x: x.get("posted_at", ""), reverse=True)
    return compressed[:max_n]


async def run_alert_service(config: Config, db: Database,
                           force: bool = False,
                           dry_run_push: bool = False) -> dict:
    """跑一次告警判定。

    per-topic frequency gate：每个 topic 可配 alert_interval_hours (0=用全局)。
    距离 topic.last_alert_checked_at 不足 interval → skip 不消耗 LLM。
    跑过的 topic（无论命中 or 0 命中）都更新 last_alert_checked_at。
    """
    async with ServiceShell("alert", config, db, force=force) as shell:
        if not shell.should_run:
            log.info("alert skipped: %s", shell.state.skip_reason)
            return {"status": "skipped", "reason": shell.state.skip_reason}

        # 全局 fallback since 起点（无 per-topic last_checked 时用）
        last = await db.last_successful_run("alert")
        if last and last.get("started_at"):
            global_since = datetime.fromisoformat(last["started_at"])
        else:
            lookback = shell.state.lookback_hours or 48.0
            global_since = datetime.now(timezone.utc) - timedelta(hours=lookback)
        global_since = global_since - timedelta(minutes=5)

        topics = await db.list_alerting_topics()
        log.info("alert run: %d topics", len(topics))

        max_msgs = config.services.alert.triage_max_messages
        dedupe_hours = config.services.alert.dedupe_hours
        now = datetime.now(timezone.utc)

        alerts_triggered = 0
        total_evaluated = 0
        topic_errors: list[str] = []
        archive_errors: list[str] = []
        topics_skipped_freq = 0
        topics_processed = 0

        for topic in topics:
            # ===== per-topic frequency gate =====
            interval_h = int(topic.get("alert_interval_hours") or 0)
            last_checked_raw = topic.get("last_alert_checked_at")
            last_checked_dt: datetime | None = None
            if last_checked_raw:
                try:
                    last_checked_dt = datetime.fromisoformat(last_checked_raw)
                except ValueError:
                    pass

            if (not force) and interval_h > 0 and last_checked_dt:
                gap = (now - last_checked_dt).total_seconds() / 3600
                if gap < interval_h:
                    log.info(
                        "topic #%s [%s]: frequency gate skipped "
                        "(gap=%.1fh < interval=%dh)",
                        topic["id"], topic["name"], gap, interval_h)
                    topics_skipped_freq += 1
                    continue

            # ===== per-topic since 起点 =====
            # 优先用 topic 自己的 last_checked（确保不漏自己的消息）
            # NULL 时用 global_since
            if last_checked_dt:
                topic_since = last_checked_dt - timedelta(minutes=5)
            else:
                topic_since = global_since

            try:
                triggered, evaluated, archive_err = await _process_topic(
                    topic, db, config,
                    since_dt=topic_since, max_msgs=max_msgs,
                    dedupe_hours=dedupe_hours, dry_run_push=dry_run_push)
                alerts_triggered += triggered
                total_evaluated += evaluated
                topics_processed += 1
                if archive_err:
                    archive_errors.append(archive_err)
            except Exception as e:
                # 单 topic 失败隔离 → 不挂整 run；其他 topic 继续跑
                err = f"topic#{topic.get('id')}[{topic.get('name')}]: {type(e).__name__}:{e}"
                log.exception("alert topic 处理失败 (隔离): %s", err)
                topic_errors.append(err)
            finally:
                # 无论 ok/exception 都更新 last_alert_checked_at
                # 这样下次 since 起点准确（不漏 + 不重）。失败时下次再 cover 同时段 = OK
                try:
                    await db.touch_topic_alert_checked(topic["id"])
                except Exception:
                    log.warning("touch_topic_alert_checked 失败 topic#%s",
                                topic.get("id"))

        shell.record(alerts_triggered=alerts_triggered)
        partial_msgs: list[str] = []
        if topic_errors:
            partial_msgs.append(
                f"topic_errors[{len(topic_errors)}]: " + " | ".join(topic_errors[:3]))
        if archive_errors:
            partial_msgs.append(
                f"archive_errors[{len(archive_errors)}]: " + " | ".join(archive_errors[:3]))
        if partial_msgs:
            await db.append_run_error(
                shell.state.run_id, " ; ".join(partial_msgs))
        result: dict = {
            "status": "success" if not partial_msgs else "partial",
            "topics_checked": len(topics),
            "topics_processed": topics_processed,
            "topics_skipped_freq": topics_skipped_freq,
            "messages_evaluated": total_evaluated,
            "alerts_triggered": alerts_triggered,
            "topic_errors": topic_errors,
            "archive_errors": archive_errors,
        }
        return result


async def _process_topic(topic: dict, db: Database, config: Config, *,
                         since_dt: datetime, max_msgs: int,
                         dedupe_hours: int, dry_run_push: bool,
                         ) -> tuple[int, int, str | None]:
    """单 topic 的告警判定流程。Returns (triggered, evaluated_count, archive_err)。

    异常向上抛，由调用方 try/except 隔离到单 topic。
    archive_err: 归档失败时返回 "topic#<id>[<name>]: <err>"，否则 None。
    """
    keywords = await db.list_topic_keywords(topic["id"])
    # alert_only=True: 只看 alert_enabled=1 的源，跳过"用户讨论区"类回声源
    sources = await db.list_topic_sources(topic["id"], alert_only=True)
    if not sources:
        log.info("topic #%s [%s]: 无 alert_enabled source, 跳过",
                 topic["id"], topic["name"])
        return 0, 0, None

    source_ids = [s["id"] for s in sources]
    skip_filter_ids = {s["id"] for s in sources
                       if s.get("skip_keyword_filter")}

    # 新 topic backfill：一次性回看 backfill_hours，跑完清零
    backfill = int(topic.get("backfill_hours") or 0)
    if backfill > 0:
        effective_since = datetime.now(timezone.utc) - timedelta(hours=backfill)
        if effective_since < since_dt:
            log.info("topic #%s [%s]: backfill window %dh (since %s)",
                     topic["id"], topic["name"], backfill,
                     effective_since.isoformat())
            await db.reset_topic_backfill(topic["id"])
        else:
            effective_since = since_dt
    else:
        effective_since = since_dt

    raw_msgs = await db.list_messages_since(source_ids, effective_since)
    matched = _filter_by_keywords(raw_msgs, keywords, skip_filter_ids)
    if not matched:
        log.info("topic #%s [%s]: 0 命中关键词", topic["id"], topic["name"])
        return 0, 0, None

    compressed = _compress_messages(matched, max_n=max_msgs)
    if len(compressed) < len(matched):
        log.info("topic #%s [%s]: 压缩 %d → %d 条",
                 topic["id"], topic["name"], len(matched), len(compressed))

    topic_for_prompt = {**topic, "_keywords": keywords}
    verdict = await judge(topic_for_prompt, compressed, config=config)
    log.info("topic #%s [%s]: worth_alert=%s headline=%r",
             topic["id"], topic["name"],
             verdict.worth_alert, verdict.headline)

    if not verdict.worth_alert or not verdict.is_valid:
        return 0, len(matched), None

    # 24h dedup
    if await db.alert_exists_recently(topic["id"], verdict.headline,
                                      hours=dedupe_hours):
        log.info("topic #%s: 24h 内已 push 过同 headline, 跳过",
                 topic["id"])
        return 0, len(matched), None

    alert_id = await db.insert_alert(
        topic_id=topic["id"],
        headline=verdict.headline,
        summary=verdict.summary,
        related_message_ids=verdict.related_message_ids,
        push_status="pending",
        push_channels=["telegram"],
    )

    success = await send_telegram(
        topic_name=topic["name"], verdict=verdict,
        messages=compressed, config=config, dry_run=dry_run_push,
    )
    await db.update_alert_push_status(
        alert_id,
        status="sent" if success else "failed",
        channels=["telegram"] if success else [],
    )

    # KB 归档（无论 push 成功与否都归档；push 失败用户仍能查到）
    archive_err: str | None = None
    try:
        append_alert_archive(topic=topic, verdict=verdict,
                             messages=compressed)
    except Exception as e:
        log.warning("KB 归档失败 topic#%s: %s", topic["id"], e)
        archive_err = f"topic#{topic['id']}[{topic['name']}]: {type(e).__name__}:{e}"

    return 1, len(matched), archive_err
