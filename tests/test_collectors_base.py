"""BaseCollector + KIND_REGISTRY 测试。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sentinel.collectors import (
    KIND_REGISTRY, register, auto_discover, BaseCollector, CollectedMessage,
)


def test_register_requires_kind():
    class Bad(BaseCollector):
        KIND = ""  # 故意为空

        async def collect(self, since):
            return; yield  # noqa

        @classmethod
        async def resolve(cls, identifier, config):
            return {}

    with pytest.raises(ValueError, match="缺少 KIND"):
        register(Bad)


def test_register_adds_to_registry():
    class Good(BaseCollector):
        KIND = "test-kind-xyz"

        async def collect(self, since):
            return; yield  # noqa

        @classmethod
        async def resolve(cls, identifier, config):
            return {"display_name": identifier}

    register(Good)
    try:
        assert KIND_REGISTRY.get("test-kind-xyz") is Good
    finally:
        KIND_REGISTRY.pop("test-kind-xyz", None)


def test_auto_discover_registers_telegram_and_rss():
    auto_discover()
    assert "telegram" in KIND_REGISTRY
    assert "rss" in KIND_REGISTRY


def test_collected_message_dataclass():
    m = CollectedMessage(
        external_id="abc",
        author="user1",
        content="hello",
        url="https://x.com/1",
        posted_at=datetime.now(timezone.utc),
        raw_json={"k": "v"},
    )
    assert m.external_id == "abc"
    assert m.raw_json == {"k": "v"}
