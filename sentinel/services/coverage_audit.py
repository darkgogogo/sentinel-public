"""Coverage audit · 周期性给每个 alert_enabled topic 算覆盖健康度。

跑：watchdog (周一 09:00) 调用，或 Web UI 手动触发。

输出：
- 写 topic_coverage_audit 表（含 LLM 给的可执行诊断 markdown）
- 返回 severity=high 的 topic 列表给 watchdog（用于 TG push）

阈值（默认全局；可被 topics.coverage_threshold_overrides JSON 覆盖）：

  指标                  high (push)    medium (banner)
  ─────────────────────────────────────────────────────
  7d 入库消息数          < 30            < 100
  platform 多样性        = 1             ≤ 2
  失败信源占比           > 50%           > 30%
  信号稀薄度             < 0.1%          < 0.5%

任一 high 命中 → severity='high'；
任一 medium 命中（无 high）→ 'medium'；
都没命中 → 'ok'（不写 audit 记录，省 db）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import aiosqlite

from sentinel.config import Config
from sentinel.db import Database
from sentinel.triage import _call_api, _call_cli


log = logging.getLogger(__name__)


# 默认阈值
DEFAULT_THRESHOLDS = {
    "high_msgs_7d": 30,
    "medium_msgs_7d": 100,
    "high_platform_count": 1,    # 仅 1 种 platform = high
    "medium_platform_count": 2,  # ≤ 2 种 = medium
    "high_failure_pct": 50.0,
    "medium_failure_pct": 30.0,
    "high_signal_ratio_pct": 0.1,
    "medium_signal_ratio_pct": 0.5,
}


@dataclass
class TopicMetrics:
    topic_id: int
    topic_name: str
    industry: str
    monitor_direction: str
    msgs_7d: int
    platform_count: int
    source_count: int
    failure_pct: float
    alerts_7d: int
    signal_ratio_pct: float  # alerts / messages * 100
    sources: list[dict]      # [{id, kind, identifier, display_name, failure_count}, ...]


def _classify_severity(m: TopicMetrics,
                      thr: dict = DEFAULT_THRESHOLDS) -> str:
    """返回 'high' / 'medium' / 'ok'。"""
    if m.source_count == 0:
        return "high"  # 没源直接告警
    high_hit = (
        m.msgs_7d < thr["high_msgs_7d"]
        or m.platform_count <= thr["high_platform_count"]
        or m.failure_pct > thr["high_failure_pct"]
        or (m.msgs_7d > 0 and m.signal_ratio_pct < thr["high_signal_ratio_pct"])
    )
    if high_hit:
        return "high"
    medium_hit = (
        m.msgs_7d < thr["medium_msgs_7d"]
        or m.platform_count <= thr["medium_platform_count"]
        or m.failure_pct > thr["medium_failure_pct"]
        or (m.msgs_7d > 0 and m.signal_ratio_pct < thr["medium_signal_ratio_pct"])
    )
    return "medium" if medium_hit else "ok"


async def _gather_metrics(db: Database, topic: dict) -> TopicMetrics:
    """对单个 topic 计算 7d 指标。"""
    async with aiosqlite.connect(db.db_path) as raw:
        raw.row_factory = aiosqlite.Row

        # 关联 sources（含 disabled 也算，更真实反映"配置 vs 健康"）
        async with raw.execute(
            """SELECT s.id, s.kind, s.identifier, s.display_name,
                      s.enabled, s.failure_count
               FROM sources s JOIN topic_sources ts ON ts.source_id=s.id
               WHERE ts.topic_id=? ORDER BY s.kind""", (topic["id"],)) as cur:
            sources = [dict(r) for r in await cur.fetchall()]

        # 7d 消息数（限制只算关联 source 的）
        if sources:
            sids = [s["id"] for s in sources]
            placeholders = ",".join("?" * len(sids))
            async with raw.execute(
                f"""SELECT COUNT(*) FROM messages
                   WHERE source_id IN ({placeholders})
                     AND collected_at >= datetime('now', '-7 days')""",
                sids) as cur:
                msgs_7d = (await cur.fetchone())[0]
        else:
            msgs_7d = 0

        # 7d alerts
        async with raw.execute(
            """SELECT COUNT(*) FROM alerts
               WHERE topic_id=? AND triggered_at >= datetime('now', '-7 days')""",
            (topic["id"],)) as cur:
            alerts_7d = (await cur.fetchone())[0]

    platform_count = len({s["kind"] for s in sources if s.get("enabled")})
    enabled_n = sum(1 for s in sources if s.get("enabled"))
    failed_n = sum(1 for s in sources
                   if s.get("enabled") and (s.get("failure_count") or 0) > 0)
    failure_pct = (failed_n / enabled_n * 100) if enabled_n else 0.0
    signal_ratio_pct = (alerts_7d / msgs_7d * 100) if msgs_7d else 0.0

    return TopicMetrics(
        topic_id=topic["id"], topic_name=topic["name"],
        industry=topic.get("industry", "general"),
        monitor_direction=topic.get("monitor_direction", ""),
        msgs_7d=msgs_7d, platform_count=platform_count,
        source_count=len(sources),
        failure_pct=round(failure_pct, 1),
        alerts_7d=alerts_7d,
        signal_ratio_pct=round(signal_ratio_pct, 3),
        sources=sources,
    )


DIAGNOSIS_PROMPT = """你是 sentinel 信源覆盖审计员。根据下面 topic 的指标，给出可执行建议。

topic: {name}
industry: {industry}
监控方向: {monitor_direction}

指标 (过去 7 天):
- 入库消息: {msgs_7d} 条
- platform 多样性: {platform_count} 种
- 信源总数: {source_count}
- 失败信源占比: {failure_pct}%
- alert 数: {alerts_7d}
- 信号稀薄度（alerts/msgs）: {signal_ratio_pct}%

当前信源:
{sources_summary}

请输出 JSON（严格格式，不要其他文字）：

{{
  "diagnosis_md": "## 诊断\\n\\n[2-4 句话说清核心问题：例如 '消息密度低 + 平台单一 + 失败率高']\\n\\n## 建议\\n\\n[1-3 条具体可执行建议]",
  "rsshub_suggestions": [
    {{"url": "https://rsshub.app/zhihu/topic/19560826", "display_name": "知乎话题/VPN", "confidence": "high"}}
  ],
  "webaccess_suggestions": [
    {{"platform": "知乎", "keyword": "VPN 推荐", "reason": "补充用户视角"}}
  ]
}}

要求：
- rsshub_suggestions: 3-5 个，URL 必须是 https://rsshub.app/* 格式，跟 topic 直接相关
- webaccess_suggestions: 2-3 个，给出搜索关键词 + 推荐用 web-access 抓哪个平台
- diagnosis_md: 简洁直接，不要客套话
"""


def _format_sources_for_prompt(sources: list[dict]) -> str:
    if not sources:
        return "（无关联信源）"
    lines = []
    for s in sources[:20]:
        flag = "✓" if s.get("enabled") else "✗"
        fail = f" failure={s['failure_count']}" if s.get("failure_count") else ""
        lines.append(f"  - [{flag}] {s['kind']}: {s['identifier']}{fail}")
    if len(sources) > 20:
        lines.append(f"  ... 还有 {len(sources) - 20} 个")
    return "\n".join(lines)


def _extract_json_obj(text: str) -> dict:
    if not text:
        return {}
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


async def _llm_diagnose(m: TopicMetrics, *, config: Config) -> dict:
    """调 Haiku 给诊断 + 建议。返回 {diagnosis_md, rsshub_suggestions, webaccess_suggestions}。"""
    prompt = DIAGNOSIS_PROMPT.format(
        name=m.topic_name, industry=m.industry,
        monitor_direction=m.monitor_direction or "(未提供)",
        msgs_7d=m.msgs_7d, platform_count=m.platform_count,
        source_count=m.source_count, failure_pct=m.failure_pct,
        alerts_7d=m.alerts_7d, signal_ratio_pct=m.signal_ratio_pct,
        sources_summary=_format_sources_for_prompt(m.sources),
    )
    model = config.models.advisor
    if config.llm.provider == "api":
        api_key = config.secrets.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return {}
        text = await asyncio.to_thread(_call_api, prompt, model=model, api_key=api_key)
    else:
        text = await asyncio.to_thread(
            _call_cli, prompt, model=model,
            cli_path=config.llm.cli_path, timeout=config.llm.cli_timeout)
    return _extract_json_obj(text)


async def audit_topic(db: Database, topic: dict, *,
                     config: Config, skip_llm: bool = False) -> dict:
    """对单个 topic 跑完整 audit 流程。返回 dict: {severity, metrics, audit_id?}"""
    m = await _gather_metrics(db, topic)
    severity = _classify_severity(m)
    metrics_dict = {
        "msgs_7d": m.msgs_7d,
        "platform_count": m.platform_count,
        "source_count": m.source_count,
        "failure_pct": m.failure_pct,
        "alerts_7d": m.alerts_7d,
        "signal_ratio_pct": m.signal_ratio_pct,
    }
    result = {"topic_id": m.topic_id, "topic_name": m.topic_name,
              "severity": severity, "metrics": metrics_dict}

    if severity == "ok":
        return result

    if skip_llm:
        # 仅写空诊断（测试 / 快速跑用）
        audit_id = await db.insert_coverage_audit(
            topic_id=m.topic_id, severity=severity, metrics=metrics_dict)
        result["audit_id"] = audit_id
        return result

    diag = await _llm_diagnose(m, config=config)
    audit_id = await db.insert_coverage_audit(
        topic_id=m.topic_id, severity=severity, metrics=metrics_dict,
        diagnosis_md=diag.get("diagnosis_md"),
        rsshub_suggestions=diag.get("rsshub_suggestions", []),
        webaccess_suggestions=diag.get("webaccess_suggestions", []),
    )
    result["audit_id"] = audit_id
    return result


async def run_coverage_audit(config: Config, db: Database, *,
                            skip_llm: bool = False) -> list[dict]:
    """跑全 audit · 返回所有 topic 的结果列表（含 severity=ok 的）。"""
    topics = await db.list_alerting_topics()
    log.info("coverage audit: %d topics", len(topics))
    results = []
    for t in topics:
        try:
            r = await audit_topic(db, t, config=config, skip_llm=skip_llm)
            log.info("topic #%s [%s] severity=%s msgs=%s plat=%s fail=%s%%",
                     t["id"], t["name"], r["severity"],
                     r["metrics"]["msgs_7d"], r["metrics"]["platform_count"],
                     r["metrics"]["failure_pct"])
        except Exception as e:
            log.exception("audit topic #%s 失败 (隔离)", t["id"])
            r = {"topic_id": t["id"], "topic_name": t["name"],
                 "severity": "error", "error": str(e)}
        results.append(r)
    return results
