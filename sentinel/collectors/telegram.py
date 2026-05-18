"""Telegram collector · telethon."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

from sentinel.collectors import register
from sentinel.collectors.base import BaseCollector, CollectedMessage


# 私有 channel identifier 是 -100xxx 数字字符串，telethon 需要 int
def _coerce_identifier(s: str):
    s = s.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return s


@register
class TelegramCollector(BaseCollector):
    KIND = "telegram"

    async def collect(self, since: datetime) -> AsyncIterator[CollectedMessage]:
        from telethon import TelegramClient
        from telethon.tl.types import Message as TgMessage

        secrets = self.config.get("secrets", {})
        api_id = secrets.get("TELEGRAM_API_ID")
        api_hash = secrets.get("TELEGRAM_API_HASH")
        if not api_id or not api_hash:
            raise RuntimeError("缺少 TELEGRAM_API_ID / TELEGRAM_API_HASH")

        from pathlib import Path
        session_path = Path.home() / "sentinel-v2" / "secrets" / "telegram.session"

        identifier = _coerce_identifier(self.source["identifier"])
        client = TelegramClient(str(session_path), int(api_id), api_hash)
        await client.start()
        try:
            entity = await client.get_entity(identifier)
            async for msg in client.iter_messages(entity, offset_date=None):
                if not isinstance(msg, TgMessage):
                    continue
                posted_at = msg.date or datetime.now(timezone.utc)
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=timezone.utc)
                if posted_at < since:
                    break  # iter_messages 默认按时间倒序，遇到老消息可早停
                content = (msg.message or "").strip()
                if not content:
                    continue
                author = None
                if msg.sender:
                    author = (getattr(msg.sender, "username", None)
                              or getattr(msg.sender, "first_name", None)
                              or str(msg.sender.id))
                url = None
                if hasattr(entity, "username") and entity.username:
                    url = f"https://t.me/{entity.username}/{msg.id}"
                yield CollectedMessage(
                    external_id=str(msg.id),
                    author=author,
                    content=content,
                    url=url,
                    posted_at=posted_at,
                    raw_json={"id": msg.id, "channel": str(identifier)},
                )
        finally:
            await client.disconnect()

    @classmethod
    async def resolve(cls, identifier: str, config: dict) -> dict:
        from telethon import TelegramClient
        secrets = config.get("secrets", {})
        api_id = secrets.get("TELEGRAM_API_ID")
        api_hash = secrets.get("TELEGRAM_API_HASH")
        if not api_id or not api_hash:
            raise ValueError("缺少 TELEGRAM_API_ID / TELEGRAM_API_HASH")

        from pathlib import Path
        session_path = Path.home() / "sentinel-v2" / "secrets" / "telegram.session"
        client = TelegramClient(str(session_path), int(api_id), api_hash)
        await client.start()
        try:
            entity = await client.get_entity(_coerce_identifier(identifier))
            display = (
                getattr(entity, "title", None)
                or getattr(entity, "username", None)
                or str(entity.id)
            )
            return {"display_name": display}
        finally:
            await client.disconnect()
