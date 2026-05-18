"""analyze.data + analyze.render + analyze service 测试。

测试覆盖：
- prompt 通用化（去 my-product/VPN 锁死）
- prompt 含 8 子标题 + 4 反向校验字段要求
- _judge_mode 自动判定
- cluster JSON 解析
- timeseries fallback 到 single（不再退 per-source）
- markdown 组装 + slug + KB 路径
- e2e（mock LLM）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sentinel.analyze.data import (
    AnalysisData, Issue, build_analysis, judge_mode,
    _SINGLE_PROMPT_TPL, _CLUSTER_PROMPT_TPL, _PER_ISSUE_PROMPT_TPL,
)
from sentinel.analyze.render import (
    build_markdown, slugify_headline, write_report_to_kb, _resolve_path,
)
from sentinel.config import Config


# ===================== prompt 通用化测试 =====================


def test_single_prompt_no_brand_hardcode():
    """v2 关键改进：prompt 必须通用化，去掉 VPN/my-product 锁死。"""
    prompt = _SINGLE_PROMPT_TPL.format(
        topic_name="X", industry="Y", monitor_direction="Z",
        period_hours=24, n=0, message_block="",
    )
    assert "my-product" not in prompt.lower()
    assert "vpn 行业" not in prompt.lower()
    # 仍含 8 子标题
    for field in ["📍 现象", "📊 规模", "💼 业务含义",
                  "❓ 反例信号", "🧪 什么会证伪",
                  "⚠️ 误判风险", "🔗 多源证实", "📌 证据"]:
        assert field in prompt


def test_per_issue_prompt_demands_8_subsections():
    """v1.2 反向校验 4 字段沿用到 v2。"""
    prompt = _PER_ISSUE_PROMPT_TPL.format(
        issue="x", topic_name="t", industry="i",
        monitor_direction="d", n=0, message_block="",
    )
    assert "my-product" not in prompt.lower()
    for field in ["📍 现象", "📊 规模", "💼 业务含义",
                  "❓ 反例信号", "🧪 什么会证伪",
                  "⚠️ 误判风险", "🔗 多源证实", "📌 证据"]:
        assert field in prompt
    # 硬性要求保留：必须主动找反向证据
    assert "必须主动找反向证据" in prompt


def test_cluster_prompt_demands_json_output():
    prompt = _CLUSTER_PROMPT_TPL.format(
        topic_name="t", industry="i", monitor_direction="d",
        n=0, message_block="",
    )
    assert "clusters" in prompt
    assert '"issue"' in prompt
    assert '"severity"' in prompt
    assert '"message_ids"' in prompt


# ===================== judge_mode =====================


def test_judge_mode_auto_short_period_few_msgs_single():
    assert judge_mode(period_hours=24, message_count=50, user_choice="auto") == "single"


def test_judge_mode_auto_long_period_uses_timeseries():
    assert judge_mode(period_hours=168, message_count=500, user_choice="auto") == "timeseries"


def test_judge_mode_user_choice_overrides():
    assert judge_mode(period_hours=24, message_count=10, user_choice="timeseries") == "timeseries"
    assert judge_mode(period_hours=168, message_count=1000, user_choice="single") == "single"


# ===================== build_analysis（mock LLM） =====================


async def test_build_analysis_empty_messages_short_circuits():
    config = Config()
    data = await build_analysis(
        topic={"name": "T", "industry": "g", "monitor_direction": ""},
        messages=[], period_hours=168, mode="auto", config=config,
    )
    assert data.message_count == 0
    assert "无相关消息" in data.headline


async def test_build_analysis_single_mode(monkeypatch):
    """mock LLM single 返回 → 应正确解析 headline + body。"""
    fake = "测试标题\n\n## 📊 本期速览\n> 内容"
    monkeypatch.setattr(
        "sentinel.analyze.data._call_cli",
        lambda prompt, *, model, cli_path, timeout: fake,
    )
    config = Config()
    data = await build_analysis(
        topic={"name": "T", "industry": "g", "monitor_direction": "d"},
        messages=[{"id": 1, "content": "x", "posted_at": "",
                   "source_id": 1, "source_display_name": "s", "author": "a"}],
        period_hours=24, mode="single", config=config,
    )
    assert data.mode_used == "single"
    assert data.headline == "测试标题"
    assert "本期速览" in data.single_body


async def test_build_analysis_timeseries_cluster_fallback_to_single(monkeypatch):
    """v2 关键改进：cluster 失败 → fallback 到 single（不再退 per-source）。"""
    call_count = {"n": 0}

    def fake_call(prompt, *, model, cli_path, timeout):
        call_count["n"] += 1
        if "聚类" in prompt:
            return ""  # cluster 失败
        return "回退后的标题\n\n## 报告内容（含 8 字段）"

    monkeypatch.setattr("sentinel.analyze.data._call_cli", fake_call)
    config = Config()
    data = await build_analysis(
        topic={"name": "T", "industry": "g", "monitor_direction": "d"},
        messages=[
            {"id": i, "content": f"msg {i}", "posted_at": "",
             "source_id": 1, "source_display_name": "s", "author": "a"}
            for i in range(5)
        ],
        period_hours=168, mode="timeseries", config=config,
    )
    assert data.mode_used == "single"  # 已 fallback
    assert "cluster_failed_fallback_to_single" in data.errors
    assert data.headline == "回退后的标题"


async def test_build_analysis_timeseries_success(monkeypatch):
    """mock cluster 返回 JSON + per-issue 返回 8 字段段。"""
    cluster_json = (
        '{"clusters": ['
        '{"issue": "议题A", "severity": "high", "message_ids": [1, 2]},'
        '{"issue": "议题B", "severity": "low", "message_ids": [3]}'
        ']}'
    )
    per_issue_body = (
        "**📍 现象**：xxx\n\n**📊 规模**：xxx\n\n"
        "**💼 业务含义**：xxx\n\n**❓ 反例信号**：xxx\n\n"
        "**🧪 什么会证伪**：xxx\n\n**⚠️ 误判风险**：xxx\n\n"
        "**🔗 多源证实**：xxx\n\n**📌 证据**：> ..."
    )

    def fake_call(prompt, *, model, cli_path, timeout):
        if "聚类" in prompt:
            return cluster_json
        return per_issue_body

    monkeypatch.setattr("sentinel.analyze.data._call_cli", fake_call)
    config = Config()
    data = await build_analysis(
        topic={"name": "T", "industry": "g", "monitor_direction": "d"},
        messages=[
            {"id": i, "content": f"msg {i}", "posted_at": "",
             "source_id": (i % 2) + 1,
             "source_display_name": f"src-{i}", "author": "a"}
            for i in [1, 2, 3]
        ],
        period_hours=168, mode="timeseries", config=config,
    )
    assert data.mode_used == "timeseries"
    assert len(data.issues) == 2
    severities = {i.severity for i in data.issues}
    assert "high" in severities and "low" in severities
    # high 议题优先排在 headline 来源
    assert "议题A" in data.headline


# ===================== render =====================


def test_slugify_headline_basic():
    assert slugify_headline("Google 限制 Gmail 存储") == "Google-限制-Gmail-存储"
    assert slugify_headline("a/b\\c?") == "a-b-c"


def test_slugify_headline_max_len():
    s = slugify_headline("超长标题" * 30)
    assert len(s) <= 40


def test_build_markdown_single_mode():
    data = AnalysisData(
        topic={"name": "T", "industry": "g"},
        period_hours=24, mode_used="single", message_count=10,
        headline="测试报告",
        single_body="## 速览\n> finding 1",
    )
    data._source_count = 2
    md = build_markdown(data)
    assert "# 测试报告" in md
    assert "速览" in md
    assert "messages=10" in md
    assert "sources=2" in md


def test_build_markdown_timeseries_mode():
    data = AnalysisData(
        topic={"name": "T", "industry": "g"},
        period_hours=168, mode_used="timeseries", message_count=50,
        headline="周报标题",
        issues=[
            Issue(name="议题A", severity="high",
                  message_ids=[1, 2],
                  body="**📍 现象**：发生了某事\n**📊 规模**：xxx"),
            Issue(name="议题B", severity="low",
                  message_ids=[3], body="**📍 现象**：日常"),
        ],
    )
    data._source_count = 3
    md = build_markdown(data)
    assert "🔴 议题 1 · 议题A" in md
    assert "🟢 议题 2 · 议题B" in md
    assert "本期速览" in md
    assert "TL;DR" in md


def test_resolve_path_weekly(tmp_path):
    data = AnalysisData(
        topic={"name": "my-product", "industry": "VPN"},
        period_hours=168,  # >=168 → 周报
        mode_used="timeseries", message_count=0, headline="x",
    )
    now = datetime(2026, 5, 15)
    path = _resolve_path(data, now, root_override=tmp_path)
    assert path.parent.name == "周报"
    assert "2026-W20-my-product-周报" in path.name


def test_resolve_path_deep(tmp_path):
    data = AnalysisData(
        topic={"name": "X", "industry": "y"},
        period_hours=48,  # < 168 → 深度
        mode_used="single", message_count=0,
        headline="测试事件",
    )
    now = datetime(2026, 5, 15)
    path = _resolve_path(data, now, root_override=tmp_path)
    assert path.parent.name == "主题深度报告"
    assert path.name.startswith("2026-05-15")
    assert "测试事件" in path.name


def test_write_report_to_kb_creates_file_with_frontmatter(tmp_path):
    data = AnalysisData(
        topic={"name": "T", "industry": "g"},
        period_hours=24, mode_used="single", message_count=1,
        headline="测试", single_body="body",
    )
    data._source_count = 1
    path = write_report_to_kb(data, root_override=tmp_path)
    assert path.exists()
    content = path.read_text()
    assert "sentinel/report-deep" in content
    assert "# 测试" in content
