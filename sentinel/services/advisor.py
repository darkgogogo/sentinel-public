"""Advisor service · 给信源运营建议。

设计.md §3.5 / §9：
- 输入：messages + alerts + user_label（默认窗口 30 天）
- Python 端聚合：每个 source 的 信噪比 = alerted_msgs / total_msgs
- 调 Haiku 4.5 把数据总结成自然语言建议
- 输出：覆盖式更新 03-主题/[topic]/信源.md 的 `## advisor 建议` section

aihot 原则 #2：数据/呈现分层
- `_collect_stats` 负责拉数据 + 聚合（无 LLM）
- `_build_prompt` + `_call_llm` 负责 LLM 调用
- `_format_section_md` 负责呈现（无数据查询）
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sentinel.config import Config
from sentinel.db import Database
from sentinel.kb import update_advisor_section
from sentinel.triage import _call_api, _call_cli


log = logging.getLogger(__name__)


@dataclass
class SourceStats:
    source_id: int
    kind: str
    identifier: str
    display_name: str
    total_messages: int = 0
    alerted_messages: int = 0
    failure_count: int = 0
    enabled: bool = True

    @property
    def signal_ratio(self) -> float:
        if self.total_messages == 0:
            return 0.0
        return self.alerted_messages / self.total_messages


@dataclass
class AdvisorResult:
    topic: dict
    window_days: int
    generated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    source_stats: list[SourceStats] = field(default_factory=list)
    total_alerts: int = 0
    true_positive_count: int = 0
    false_positive_count: int = 0
    unlabeled_count: int = 0
    llm_recommendations: str = ""  # LLM 自然语言建议
    section_md: str = ""           # 写入信源.md 的完整 section markdown


# ===================== 数据层 =====================


async def _collect_stats(topic: dict, db: Database,
                         since: datetime,
                         sources: list[dict]) -> tuple[
                             list[SourceStats], list[dict]]:
    """拉数据 + 聚合。Returns (source_stats, alerts)。"""
    source_ids = [s["id"] for s in sources]
    msg_count_map = await db.messages_count_per_source(source_ids, since)
    alerts = await db.alerts_for_topic(topic["id"], since)

    # 计算每个 source 被 alert 引用了多少 distinct message_ids
    alerted_per_source: dict[int, set] = {sid: set() for sid in source_ids}
    msg_to_source: dict[int, int] = {}
    # 拉所有相关 messages 的 source_id 索引
    related_ids: set[int] = set()
    for a in alerts:
        try:
            ids = json.loads(a.get("related_message_ids") or "[]")
            related_ids.update(int(i) for i in ids
                              if str(i).lstrip("-").isdigit())
        except Exception:
            continue
    if related_ids:
        import aiosqlite
        async with aiosqlite.connect(db.db_path) as raw:
            ph = ",".join("?" * len(related_ids))
            async with raw.execute(
                f"SELECT id, source_id FROM messages WHERE id IN ({ph})",
                list(related_ids)) as cur:
                for r in await cur.fetchall():
                    msg_to_source[int(r[0])] = int(r[1])

    for a in alerts:
        try:
            ids = json.loads(a.get("related_message_ids") or "[]")
        except Exception:
            continue
        for mid in ids:
            mid = int(mid)
            sid = msg_to_source.get(mid)
            if sid is not None and sid in alerted_per_source:
                alerted_per_source[sid].add(mid)

    stats: list[SourceStats] = []
    for s in sources:
        sid = s["id"]
        stats.append(SourceStats(
            source_id=sid, kind=s["kind"],
            identifier=s["identifier"],
            display_name=s.get("display_name") or s["identifier"],
            total_messages=msg_count_map.get(sid, 0),
            alerted_messages=len(alerted_per_source.get(sid, set())),
            failure_count=s.get("failure_count", 0),
            enabled=bool(s.get("enabled", 1)),
        ))
    # 按信噪比降序
    stats.sort(key=lambda x: x.signal_ratio, reverse=True)
    return stats, alerts


# ===================== LLM 层 =====================


_ADVISOR_PROMPT_TPL = """你是信源运营顾问。请基于以下统计数据，为「{topic_name}」主题（行业：{industry}）的信源运营给出**简洁、可操作**的建议。

监控方向：{monitor_direction}
统计窗口：过去 {window_days} 天
总告警数：{total_alerts}（用户标 ⭐ 准 = {tp}，🚫 误报 = {fp}，未标 = {unlabeled}）

## 信源信噪比表（按命中率降序）

{stats_table}

## 建议要求

请以 markdown 列表形式输出**精炼建议**（200-400 字内），不要再加 markdown 标题。涵盖：

1. **建议关闭/降权的 source**：信噪比 < 0.01 或 failure_count 高、且无独家价值 → 列具体 source 名
2. **建议加权/留住的 source**：信噪比 > 0.2 或 alerted_messages 多 → 列具体 source 名
3. **建议添加 / 删除关键词**（如有规律）：根据 true_positive vs false_positive 模式推荐
4. **监控方向 / 主题描述**调整建议（如有必要）
5. **数据不足或盲区**：哪些 source 数据太少无法判断 → 列出建议观察更久

格式示例：

- 建议关闭：source-A · 信噪比 0.003（30 天 0 告警引用）· 噪音多无价值
- 建议加权：source-B · 信噪比 0.32（命中 12 次）· 持续高质量
- 推荐关键词：增加 "新关键词1", "新关键词2"
- 盲区：source-C 30 天仅 3 条消息，建议观察 30 天再评估

不要编造数据中没出现的 source / 数字。"""


def _build_prompt(topic: dict, window_days: int,
                  result: AdvisorResult) -> str:
    stats_lines = []
    for s in result.source_stats:
        stats_lines.append(
            f"- [{s.kind}] {s.display_name} (#{s.source_id})"
            f" · 总消息 {s.total_messages}"
            f" · 命中告警 {s.alerted_messages}"
            f" · 信噪比 {s.signal_ratio:.4f}"
            f" · 失败 {s.failure_count}"
            f"{' · 已禁用' if not s.enabled else ''}"
        )
    return _ADVISOR_PROMPT_TPL.format(
        topic_name=topic["name"],
        industry=topic.get("industry", "general"),
        monitor_direction=topic.get("monitor_direction", ""),
        window_days=window_days,
        total_alerts=result.total_alerts,
        tp=result.true_positive_count,
        fp=result.false_positive_count,
        unlabeled=result.unlabeled_count,
        stats_table="\n".join(stats_lines) if stats_lines else "（无 source）",
    )


async def _call_llm(prompt: str, *, config: Config) -> str:
    model = config.models.advisor
    if config.llm.provider == "api":
        api_key = config.secrets.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return ""
        return await asyncio.to_thread(_call_api, prompt,
                                       model=model, api_key=api_key)
    return await asyncio.to_thread(
        _call_cli, prompt,
        model=model, cli_path=config.llm.cli_path,
        timeout=config.llm.cli_timeout)


# ===================== 呈现层 =====================


def _format_section_md(result: AdvisorResult) -> str:
    """生成完整 advisor section markdown（含元信息 + LLM 建议 + 数据表）。"""
    lines: list[str] = []
    lines.append("## advisor 建议")
    lines.append("")
    lines.append(
        f"> 最近更新: {result.generated_at.strftime('%Y-%m-%d %H:%M UTC')}"
        f" · 窗口 {result.window_days} 天 · 由 advisor service 自动写入"
    )
    lines.append("")

    # 反馈统计
    if result.total_alerts > 0:
        lines.append(
            f"**告警反馈**：{result.total_alerts} 条告警 · "
            f"⭐ 准 {result.true_positive_count} · "
            f"🚫 误报 {result.false_positive_count} · "
            f"未标 {result.unlabeled_count}"
        )
        lines.append("")

    # LLM 建议
    if result.llm_recommendations:
        lines.append(result.llm_recommendations.strip())
    else:
        lines.append("> ⚠ LLM 调用失败，无 AI 建议（数据表仍可参考）")
    lines.append("")

    # 数据表（精简）
    lines.append("### 信源数据（窗口内）")
    lines.append("")
    lines.append("| source | 总消息 | 命中告警 | 信噪比 | 失败 | 状态 |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for s in result.source_stats:
        status = "enabled" if s.enabled else "**disabled**"
        lines.append(
            f"| [{s.kind}] {s.display_name} | {s.total_messages} | "
            f"{s.alerted_messages} | {s.signal_ratio:.4f} | "
            f"{s.failure_count} | {status} |"
        )
    lines.append("")
    return "\n".join(lines)


# ===================== 主入口 =====================


async def run_advisor_service(config: Config, db: Database, *,
                              topic_name: str,
                              window_days: int | None = None) -> dict:
    """跑一次 advisor scan。"""
    topics = await db.list_all_topics()
    topic = next((t for t in topics if t["name"] == topic_name), None)
    if not topic:
        return {"status": "failed", "error": f"topic 不存在: {topic_name!r}"}

    window_days = window_days or config.services.advisor.feedback_window_days
    run_id = await db.insert_run(
        service="advisor", lookback_hours=float(window_days * 24),
        status="running")

    try:
        sources = await db.list_topic_sources(topic["id"])
        # 含 disabled 的也要纳入（让 advisor 看到死掉的源）
        # list_topic_sources 已经过滤 enabled=1，这里再补一遍 disabled 的
        all_sources = await db.list_all_sources()
        topic_source_ids = {s["id"] for s in sources}
        # 加入 disabled 但仍 linked 的（用 topic_sources 表手动 query）
        import aiosqlite
        async with aiosqlite.connect(db.db_path) as raw:
            raw.row_factory = aiosqlite.Row
            async with raw.execute(
                """SELECT s.* FROM sources s JOIN topic_sources ts ON ts.source_id=s.id
                   WHERE ts.topic_id=? AND s.enabled=0""",
                (topic["id"],)) as cur:
                disabled_linked = [dict(r) for r in await cur.fetchall()]
        all_topic_sources = sources + disabled_linked

        since = datetime.now(timezone.utc) - timedelta(days=window_days)
        source_stats, alerts = await _collect_stats(
            topic, db, since, all_topic_sources)

        # alerts 标签统计
        tp = sum(1 for a in alerts if a.get("user_label") == "true_positive")
        fp = sum(1 for a in alerts if a.get("user_label") == "false_positive")
        unlabeled = sum(1 for a in alerts if not a.get("user_label"))

        result = AdvisorResult(
            topic=topic, window_days=window_days,
            source_stats=source_stats,
            total_alerts=len(alerts),
            true_positive_count=tp, false_positive_count=fp,
            unlabeled_count=unlabeled,
        )

        # 调 LLM
        prompt = _build_prompt(topic, window_days, result)
        result.llm_recommendations = await _call_llm(prompt, config=config)

        # 组装 section
        result.section_md = _format_section_md(result)

        # 写 KB（覆盖式更新 advisor 段）
        kb_path = update_advisor_section(
            topic=topic, sources=all_topic_sources,
            section_md=result.section_md)

        await db.update_run(
            run_id, status="success",
            messages_collected=sum(s.total_messages for s in source_stats))

        return {
            "status": "success",
            "kb_path": str(kb_path),
            "topic": topic["name"],
            "window_days": window_days,
            "source_count": len(source_stats),
            "total_alerts": len(alerts),
            "llm_ok": bool(result.llm_recommendations),
        }
    except Exception as e:
        log.exception("advisor 失败")
        await db.update_run(run_id, status="failed", error=str(e))
        return {"status": "failed", "error": str(e)}
