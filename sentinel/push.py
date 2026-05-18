"""Push · Telegram bot 发送（先实现 telegram，后续可扩 email/slack）。

格式（继承 v1.1 优化）：
- summary 用 `>` blockquote 多行
- 证据按频道分组：`🔹 *Source* · N 条` + 列前 2 条 + 单 wikilink（避免链接刷屏）
- 主题+时间合并 `▎ℹ️ topic · MM-DD HH:MM` 一行收尾
- 单 push 长度 ≤ Telegram 4096 字符限制
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import httpx

from sentinel.config import Config
from sentinel.triage import TriageVerdict


log = logging.getLogger(__name__)


# Telegram MarkdownV2 必须转义的字符
_MD_V2_ESCAPE = set("_*[]()~`>#+-=|{}.!\\")


def _escape_md(s: str) -> str:
    """MarkdownV2 严格转义 · 不转移 *_ 等格式控制字符以外的所有 ascii 标点。"""
    out = []
    for ch in s:
        if ch in _MD_V2_ESCAPE:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _group_evidence_by_source(messages: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for m in messages:
        src = (m.get("source_display_name")
               or m.get("source_identifier")
               or m.get("source_kind", "?"))
        grouped.setdefault(src, []).append(m)
    return grouped


def _format_telegram_text(topic_name: str, verdict: TriageVerdict,
                         messages: list[dict],
                         max_chars: int = 3500) -> str:
    """组装 Telegram MarkdownV2 文本。"""
    related = set(verdict.related_message_ids)
    evidence_msgs = [m for m in messages if m["id"] in related]
    if not evidence_msgs:
        evidence_msgs = messages  # fallback

    lines: list[str] = []

    # headline
    lines.append(f"🚨 *{_escape_md(verdict.headline)}*")
    lines.append("")

    # summary as blockquote
    for line in verdict.summary.split("\n"):
        lines.append(f">{_escape_md(line)}")
    lines.append("")

    # 证据按频道分组
    grouped = _group_evidence_by_source(evidence_msgs)
    lines.append(f"📊 *证据 · 跨 {len(grouped)} 个频道*")

    for src, ms in grouped.items():
        lines.append(f"🔹 *{_escape_md(src)}* · {len(ms)} 条")
        for m in ms[:2]:
            content = (m.get("content") or "").replace("\n", " ").strip()
            snippet = content[:120] + ("…" if len(content) > 120 else "")
            author = m.get("author") or "?"
            lines.append(f"  • {_escape_md(author)}: {_escape_md(snippet)}")
        # 单 wikilink（避免链接刷屏）
        url = next((m.get("url") for m in ms if m.get("url")), None)
        if url:
            lines.append(f"  🔗 {_escape_md(url)}")

    # 收尾
    now = datetime.now().strftime("%m-%d %H:%M")
    lines.append("")
    lines.append(f"▎ℹ️ {_escape_md(topic_name)} · {_escape_md(now)}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars - 20] + "\n\n…\\(截断\\)"
    return text


async def send_telegram(topic_name: str, verdict: TriageVerdict,
                       messages: list[dict],
                       config: Config, *, dry_run: bool = False) -> bool:
    """发 Telegram bot push · 成功 True，失败 False（不抛）。

    dry_run=True 时只 log，不真发。
    """
    secrets = config.secrets
    bot_token = secrets.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = secrets.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        log.warning("缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID · 跳过 push")
        return False

    text = _format_telegram_text(topic_name, verdict, messages)

    if dry_run:
        log.info("[DRY-RUN] Telegram push (%d chars):\n%s", len(text), text)
        return True

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            })
        if resp.status_code == 200:
            return True
        log.warning("TG push 失败 status=%d body=%s",
                    resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        log.warning("TG push 异常: %s", e)
        return False
