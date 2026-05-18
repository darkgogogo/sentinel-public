"""triage.py · prompt 构造 + JSON 解析 + LLM mock 测试。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from sentinel.config import Config
from sentinel.triage import (
    PROMPT_TEMPLATE, TriageVerdict, _extract_json,
    _format_message_block, judge,
)


def test_format_message_block_uses_source_display_name():
    messages = [
        {"id": 1, "posted_at": "2026-05-15T10:00:00",
         "source_display_name": "Solidot", "author": "alice",
         "content": "test event"},
    ]
    block = _format_message_block(messages)
    assert "[ID:1]" in block
    assert "[Solidot]" in block
    assert "alice: test event" in block


def test_format_message_block_truncates_long_content():
    messages = [
        {"id": 1, "posted_at": "2026-05-15",
         "source_display_name": "x", "author": "a",
         "content": "a" * 1000},
    ]
    block = _format_message_block(messages)
    assert len(block) < 700  # 500 chars max + 余量


def test_extract_json_plain():
    assert _extract_json('{"worth_alert": true}') == {"worth_alert": True}


def test_extract_json_with_code_fence():
    text = '```json\n{"worth_alert": false}\n```'
    assert _extract_json(text) == {"worth_alert": False}


def test_extract_json_with_noise_around():
    text = 'Sure, here is the JSON:\n{"worth_alert": true, "headline": "x"}\n'
    assert _extract_json(text) == {"worth_alert": True, "headline": "x"}


def test_extract_json_empty_on_garbage():
    assert _extract_json("not even close") == {}


def test_extract_json_array_picks_first_worth_alert_true():
    """LLM 偶尔返回数组 — 取第一个 worth_alert=true 的元素。"""
    text = '[{"worth_alert": false, "headline": "a"}, {"worth_alert": true, "headline": "b"}]'
    obj = _extract_json(text)
    assert obj.get("worth_alert") is True
    assert obj.get("headline") == "b"


def test_extract_json_array_with_fence():
    """实测 LLM 真返回的形态：```json\\n[{...}]\\n```"""
    text = '```json\n[\n  {\n    "worth_alert": true,\n    "headline": "X",\n    "summary": "y", "related_message_ids": [1]\n  }\n]\n```'
    obj = _extract_json(text)
    assert obj.get("worth_alert") is True
    assert obj.get("headline") == "X"


def test_prompt_uses_industry_and_topic_name():
    """v2 通用化：prompt 应含 industry / topic 名，不锁死 VPN。"""
    prompt = PROMPT_TEMPLATE.format(
        industry="教育", topic_name="双减政策",
        monitor_direction="关注 K12 招生", keywords="双减, 招生",
        n=0, message_block="",
    )
    assert "教育" in prompt
    assert "双减政策" in prompt
    assert "双减, 招生" in prompt
    # 不应硬编码 VPN/my-product
    assert "my-product" not in prompt.lower()
    assert "vpn 行业" not in prompt.lower()


async def test_judge_returns_no_alert_on_empty_messages():
    config = Config()
    verdict = await judge({"name": "x", "industry": "g",
                          "monitor_direction": ""}, [], config=config)
    assert verdict.worth_alert is False


async def test_judge_parses_llm_json(monkeypatch):
    """mock LLM 返回 JSON，verdict 应该正确解析。"""
    fake_response = (
        '{"worth_alert": true, "headline": "测试事件",'
        ' "summary": "短描述", "related_message_ids": [1, 3]}'
    )
    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda prompt, *, model, cli_path, timeout: fake_response,
    )
    config = Config()
    verdict = await judge(
        {"id": 1, "name": "Test", "industry": "test",
         "monitor_direction": "x"},
        [{"id": 1, "content": "y", "posted_at": "2026-05-15",
          "source_display_name": "src", "author": "a"}],
        config=config,
    )
    assert verdict.worth_alert is True
    assert verdict.headline == "测试事件"
    assert verdict.related_message_ids == [1, 3]


async def test_judge_graceful_on_llm_timeout(monkeypatch):
    """LLM 返回空字符串（cli timeout 等）→ verdict worth_alert=False，不抛。"""
    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda prompt, *, model, cli_path, timeout: "",
    )
    config = Config()
    verdict = await judge(
        {"id": 1, "name": "T", "industry": "g", "monitor_direction": ""},
        [{"id": 1, "content": "x", "posted_at": "2026-05-15",
          "source_display_name": "s", "author": "a"}],
        config=config,
    )
    assert verdict.worth_alert is False


async def test_judge_graceful_on_invalid_json(monkeypatch):
    """LLM 返回非 JSON → verdict worth_alert=False。"""
    monkeypatch.setattr(
        "sentinel.triage._call_cli",
        lambda prompt, *, model, cli_path, timeout: "this is not json",
    )
    config = Config()
    verdict = await judge(
        {"id": 1, "name": "T", "industry": "g", "monitor_direction": ""},
        [{"id": 1, "content": "x", "posted_at": "2026-05-15",
          "source_display_name": "s", "author": "a"}],
        config=config,
    )
    assert verdict.worth_alert is False
    assert "not json" in verdict.raw_response
