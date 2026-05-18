"""file_inbox collector · 监视 KB inbox 目录把 markdown 入库。

定位：v1 social_scan SocialCollector 的精简复活，但去掉 platform 维度限制。
任何手动抓回的 markdown（web-access subagent / 手动复制粘贴 / 其他工具产出）
都可以落到 inbox/<topic_slug>/<name>.md，sentinel collect 自动 ingest 入 db。

工作流：
1. 用户跟 LLM 说"用 web-access 抓 X 进 topic Y 的 inbox"
2. LLM 调 web-access subagent 抓内容 → 落到 ~/.kb/.../Sentinel/inbox/<slug>/<file>.md
3. 用户在 /sources 加一个 source: kind=inbox, identifier=<topic_slug>
   （inbox 是按 topic_slug 隔离的目录，不是按平台）
4. 该 source 必须 link 到对应 topic
5. 下次 collect 跑 → InboxCollector ingest 该目录下 *.md → 改名 *.md.ingested

frontmatter 约定（可选）:
  ---
  platform: 知乎               # 来源平台
  source_url: https://...       # 原帖 URL
  collected_at: 2026-05-16
  ---
  内容 ...

整个 .md 入库为 1 条 message（不切分），content 含 frontmatter + body。
external_id = 文件名 (无扩展名)，防同 topic 重名。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from sentinel.collectors import register
from sentinel.collectors.base import BaseCollector, CollectedMessage
from sentinel.config import KB_ROOT


log = logging.getLogger(__name__)


INBOX_ROOT = KB_ROOT / "inbox"


def _topic_slug(name: str) -> str:
    """跟 sentinel.kb.topic_slug 一致逻辑。"""
    return re.sub(r"\s+", "-", name.strip())


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """剥 frontmatter, 返回 (meta_dict, body_text)。"""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fm = text[4:end]
    body = text[end + 5:]
    meta = {}
    for line in fm.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body


@register
class InboxCollector(BaseCollector):
    """监视 ~/.kb/.../inbox/<topic_slug>/*.md 入库 (但不递归 .md.ingested)。

    identifier 约定为 topic_slug (如 'VPN' / 'Solidot-科技动态')。
    """
    KIND = "inbox"

    async def collect(self, since: datetime) -> AsyncIterator[CollectedMessage]:
        slug = (self.source.get("identifier") or "").strip()
        if not slug:
            raise RuntimeError("inbox collector 需要 identifier 作为 topic_slug")
        topic_dir = INBOX_ROOT / slug
        if not topic_dir.exists():
            log.info("inbox dir 不存在: %s", topic_dir)
            return

        # 扫所有 .md（排除 .md.ingested 标记）
        for p in sorted(topic_dir.glob("*.md")):
            if p.name.endswith(".md.ingested"):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception as e:
                log.warning("读 %s 失败: %s", p, e)
                continue

            meta, body = _parse_frontmatter(text)
            posted_at: datetime
            collected_at = meta.get("collected_at") or meta.get("date")
            if collected_at:
                try:
                    posted_at = datetime.fromisoformat(collected_at)
                    if posted_at.tzinfo is None:
                        posted_at = posted_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    posted_at = datetime.fromtimestamp(
                        p.stat().st_mtime, tz=timezone.utc)
            else:
                posted_at = datetime.fromtimestamp(
                    p.stat().st_mtime, tz=timezone.utc)

            if posted_at < since:
                continue

            external_id = p.stem  # 文件名作 id（同 topic 不重名）
            content = (
                f"[{meta.get('platform', 'inbox')}] " +
                (meta.get('source_url', '')) + "\n\n" +
                body
            ).strip()

            yield CollectedMessage(
                external_id=external_id,
                author=meta.get("author"),
                content=content,
                url=meta.get("source_url"),
                posted_at=posted_at,
                raw_json={"platform": meta.get("platform", "inbox"),
                          "file": str(p.relative_to(INBOX_ROOT))},
            )

            # ingest 完改名（防下次重复）
            try:
                p.rename(p.with_suffix(".md.ingested"))
            except OSError as e:
                log.warning("rename %s → .ingested 失败: %s", p, e)

    @classmethod
    async def resolve(cls, identifier: str, config: dict) -> dict:
        slug = identifier.strip()
        if not slug:
            raise ValueError("inbox identifier 不能为空（应为 topic slug）")
        topic_dir = INBOX_ROOT / slug
        topic_dir.mkdir(parents=True, exist_ok=True)
        return {"display_name": f"inbox·{slug}"}
