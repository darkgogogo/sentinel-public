"""Keyword advisor · LLM 推荐 topic 的关键词。

针对 v2 alert 两层过滤 (substring → LLM triage) 的第一层：keyword 决定
哪些消息进 LLM。current 关键词太少或太宽时:
- 过 keyword filter 的消息一堆噪音 → LLM 烧 token + alert 假阳性高
- 反过来太严 → 漏报 (false negative)

推荐逻辑:
- 输入: topic 元信息 + 当前关键词 + 最近一批样本消息 (含命中 / 未命中)
- LLM 输出: 5-15 推荐关键词 + 每个一句理由 + confidence
- 过滤跟当前已有关键词重复的 + 太短的 / 太宽泛的
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import aiosqlite

from sentinel.config import Config
from sentinel.db import Database
from sentinel.triage import _call_api, _call_cli


log = logging.getLogger(__name__)


PROMPT_TEMPLATE = """你是 sentinel 信息监控的关键词优化助手。

任务: 根据 topic 配置 + 最近真实采集的消息样本，推荐 substring 关键词。

## 工作原理 (你需要懂)
sentinel alert service 每次跑会做两层过滤：
1. **第一层 substring 过滤**: 只让消息文本里包含**任一关键词**的进入下一层
2. **第二层 LLM triage**: 对过滤后消息判断 worth_alert

你推荐的关键词决定第一层过滤的精度。

## Topic
- 名称: {name}
- 行业: {industry}
- 监控方向: {monitor_direction}

## 当前已有关键词 ({n_existing} 个)
{existing_keywords}

## 最近采集的消息样本 ({n_samples} 条, 含命中和未命中的)
{message_samples}

## 推荐规则

1. 关键词应该是**短词 / phrase** (2-10 字最佳)，太长会过严
2. 优先**专有名词、术语、产品名、人物**，不要太宽泛的词如"消息""新闻"
3. 中英文都覆盖 (如果该 topic 跨语言)
4. **不要推荐已有关键词**: {existing_csv}
5. 看消息样本: 哪些是该 topic 的真信号 → 这些消息有什么特征词
6. 推荐 5-15 个，按 confidence 排序

## 输出 JSON 数组 (仅 JSON, 不要其他文本)

[
  {{"keyword": "VLESS", "reason": "VPN 协议名, 监控方向核心议题", "confidence": "high"}},
  {{"keyword": "工信部", "reason": "政策监管机构, 出新规时高频", "confidence": "high"}},
  {{"keyword": "翻墙", "reason": "中文圈惯用词，匹配大量真消息", "confidence": "medium"}}
]
"""


def _extract_json_list(text: str) -> list:
    if not text:
        return []
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    else:
        m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
        if m:
            text = m.group(0)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, list) else []
    except json.JSONDecodeError:
        return []


def _normalize_candidate(raw: dict, existing_set: set[str]) -> dict | None:
    """validate + 过滤已有 + 过短过长。"""
    if not isinstance(raw, dict):
        return None
    kw = str(raw.get("keyword", "")).strip()
    # 过滤：太短 (1 字) / 太长 (>30) / 跟已有重 (case-insensitive)
    if len(kw) < 2 or len(kw) > 30:
        return None
    if kw.lower() in existing_set:
        return None
    confidence = str(raw.get("confidence", "medium")).strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    reason = str(raw.get("reason", "")).strip()[:200]
    return {"keyword": kw, "reason": reason, "confidence": confidence}


def _format_messages_for_prompt(messages: list[dict],
                                max_content_chars: int = 220) -> str:
    """格式化消息样本作为 prompt 输入。"""
    if not messages:
        return "(无消息样本)"
    lines = []
    for i, m in enumerate(messages[:50], 1):
        content = (m.get("content") or "").strip()
        if len(content) > max_content_chars:
            content = content[:max_content_chars] + "…"
        source_kind = m.get("source_kind", "?")
        flag = "⭐" if m.get("alert_id") else " "
        lines.append(f"  {flag} [{source_kind}] {content}")
    return "\n".join(lines)


async def _sample_messages(db: Database, topic_id: int,
                          n_alerted: int = 15,
                          n_unalerted: int = 35) -> list[dict]:
    """采样 alert 命中的 + 未命中的消息混合 (帮 LLM 区分有效信号 vs 噪音)。"""
    async with aiosqlite.connect(db.db_path) as raw:
        raw.row_factory = aiosqlite.Row

        # 拉 topic 关联 source ids
        async with raw.execute(
            "SELECT source_id FROM topic_sources WHERE topic_id=?",
            (topic_id,)) as cur:
            source_ids = [r[0] for r in await cur.fetchall()]
        if not source_ids:
            return []
        placeholders = ",".join("?" * len(source_ids))

        # alert 命中过的 messages (related_message_ids 在 alerts 表)
        async with raw.execute(
            f"""SELECT m.*, s.kind AS source_kind, a.id AS alert_id
                FROM messages m
                JOIN sources s ON s.id=m.source_id
                JOIN alerts a ON a.topic_id=?
                  AND instr(a.related_message_ids, '"' || m.id || '"') > 0
                  AND a.user_label IS NOT 'false_positive'
                WHERE m.source_id IN ({placeholders})
                ORDER BY m.posted_at DESC LIMIT ?""",
            [topic_id] + source_ids + [n_alerted]) as cur:
            alerted = [dict(r) for r in await cur.fetchall()]

        alerted_ids = {m["id"] for m in alerted}

        # 最近未命中 alert 的 messages (代表"过了 keyword 但 LLM 没 alert" 的噪音)
        ex_placeholders = ",".join("?" * len(alerted_ids)) if alerted_ids else "0"
        ex_filter = (f"AND m.id NOT IN ({ex_placeholders})"
                     if alerted_ids else "")
        async with raw.execute(
            f"""SELECT m.*, s.kind AS source_kind, NULL AS alert_id
                FROM messages m
                JOIN sources s ON s.id=m.source_id
                WHERE m.source_id IN ({placeholders}) {ex_filter}
                  AND m.collected_at >= datetime('now', '-14 days')
                ORDER BY m.posted_at DESC LIMIT ?""",
            source_ids + list(alerted_ids) + [n_unalerted]) as cur:
            unalerted = [dict(r) for r in await cur.fetchall()]

    return alerted + unalerted


async def recommend_keywords(topic_id: int, db: Database, *,
                            config: Config) -> dict:
    """主入口。返回 {candidates: [...], samples_n: N, existing_n: N}。"""
    topic = await db.get_topic(topic_id)
    if not topic:
        return {"error": "topic 不存在"}

    existing = await db.list_topic_keywords(topic_id)
    existing_set = {k.lower() for k in existing}

    samples = await _sample_messages(db, topic_id)
    samples_block = _format_messages_for_prompt(samples)

    prompt = PROMPT_TEMPLATE.format(
        name=topic["name"],
        industry=topic.get("industry", "general"),
        monitor_direction=topic.get("monitor_direction") or "(未设置)",
        n_existing=len(existing),
        existing_keywords=", ".join(existing) if existing else "(无)",
        existing_csv=", ".join(existing) if existing else "(无)",
        n_samples=len(samples),
        message_samples=samples_block,
    )

    model = config.models.advisor
    if config.llm.provider == "api":
        api_key = config.secrets.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return {"error": "缺 ANTHROPIC_API_KEY"}
        text = await asyncio.to_thread(
            _call_api, prompt, model=model, api_key=api_key)
    else:
        text = await asyncio.to_thread(
            _call_cli, prompt, model=model,
            cli_path=config.llm.cli_path, timeout=config.llm.cli_timeout)

    raw_list = _extract_json_list(text)
    if not raw_list:
        log.warning("recommend_keywords: 非 JSON 数组 · text=%r", text[:200])
        return {"candidates": [], "samples_n": len(samples),
                "existing_n": len(existing), "error": "LLM 输出格式不对"}

    candidates: list[dict] = []
    seen_kws: set[str] = set(existing_set)
    for item in raw_list:
        norm = _normalize_candidate(item, seen_kws)
        if not norm:
            continue
        seen_kws.add(norm["keyword"].lower())
        candidates.append(norm)

    return {"candidates": candidates, "samples_n": len(samples),
            "existing_n": len(existing)}
