"""analyze.data · 数据/LLM 层 · 拉消息 → 调 LLM → 返回结构化 AnalysisData。

aihot 原则 #2 落地点：**只关心数据**，不组装 markdown，不写文件。

v2 与 v1.2 的差异：
- prompt 通用化（去 VPN/letsvpn 锁死，参数化 industry + topic_name）
- 沿用 v1.2 反向校验 8 字段（❓反例 / 🧪证伪 / ⚠️误判 / 🔗多源 + 4 原字段）
- 模板从 4 个收缩为 3 个：_SINGLE / _CLUSTER / _PER_ISSUE
- **没有 _PER_SOURCE fallback**——cluster 失败时回退到 _SINGLE，**不再绕过 8 字段**
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sentinel.config import Config
from sentinel.triage import _call_api, _call_cli, _extract_json


log = logging.getLogger(__name__)


# ===================== Prompt 模板 =====================

_SINGLE_PROMPT_TPL = """你是行业舆情分析师。请基于以下消息为「{topic_name}」主题（行业：{industry}）写一份**结构化深度报告**，覆盖过去 {period_hours} 小时。

监控方向：{monitor_direction}

## 输出格式（严格遵守）

第 1 行：报告标题（一句话核心洞察，30 字内，不带 # 前缀）

然后空一行，正文严格按以下结构（markdown）：

```
## 📊 本期速览

> **关键发现 1**：xxx
> **关键发现 2**：xxx
> **关键发现 3**：xxx

📈 N 条消息 · M 个议题 · 横跨 X 个信源

---

## 🔴 议题 1 · 议题精炼名

**📍 现象**：（一句话事件描述）

**📊 规模**：（涉及频道/账号/用户量级/时间窗）

**💼 业务含义**：（对该主题的监控对象——产品体验/竞品格局/上游基建等——的影响判断）

**❓ 反例信号**：（是否有信息源说"没发生 / 影响被夸大 / 已恢复"？无则写"未发现反向证据"，30-60 字）

**🧪 什么会证伪**：（12 个月内什么数据/事件能证明这个 finding 是误判？30-60 字）

**⚠️ 误判风险**：（如果这个 finding 后来发现错了，最可能原因是？枚举：采样偏置 / 单源 / 时间窗口太短 / 用户表达放大 / 信源立场偏向 / 等，30-60 字）

**🔗 多源证实**：（是否被 ≥2 个独立信源证实？是 → 列信源名；否 → 标"单源风险，待补充验证"，30-60 字）

**📌 证据**：
> 内容片段 — 作者 · 频道 · 时间
> 内容片段 — 作者 · 频道 · 时间

---

## 🟡 议题 2 · ...

---

## 🎯 行动建议（如无可省）

---

## 📝 TL;DR

一段 100 字内总结，回答："本期最该关注什么"。
```

## 严重度规则
- 🔴 高：直接威胁监控对象的核心可用性 / 大规模事件 / 头部官方公告
- 🟡 中：值得关注但影响有限 / 跨源已验证的趋势
- 🟢 低：日常背景信号 / 单一来源参考

## 硬性要求
- 不编造未在消息中出现的事实
- 引用原文严格用 `> 内容 — 作者 · 频道 · 时间` 格式
- 议题数 2-5 个（按消息密度自动决定）
- 每议题 **8 个字段全部出现**（📍现象 / 📊规模 / 💼业务含义 / ❓反例信号 / 🧪什么会证伪 / ⚠️误判风险 / 🔗多源证实 / 📌证据），缺一不可
- ❓ 反例 / 🧪 证伪 / ⚠️ 误判 三个反向校验字段 **必须主动找**，不能简单写"无"——除非真的从消息列表里穷举证实了无反向证据
- 🔗 多源证实 必须 **基于真实信源**，跨 source 数；如只 1 个 source 必须标"单源风险"

## 消息列表（共 {n} 条）

{message_block}
"""


_CLUSTER_PROMPT_TPL = """你是行业舆情分析师。请把下面这批消息按"议题"聚类，并为每个议题打一个**严重度标签**。

监控主题：{topic_name}（行业：{industry}）
监控方向：{monitor_direction}

## 严重度规则
- `high`：直接威胁监控对象核心可用性 / 大规模事件 / 头部官方公告
- `medium`：值得关注但影响有限 / 跨源已验证的趋势
- `low`：日常背景信号 / 单一来源参考 / 日常聊天

## 聚类要求
- 议题数量按消息密度自动决定，2-8 个之间
- 议题标题精炼（10-20 字）
- 单条消息或日常聊天 → 归到"其他/日常讨论"议题（severity=low）
- 严格互斥：每个 message_id 只出现在一个 cluster 里
- 严格只输出 JSON，不要 markdown 代码块包裹，不要解释文字

## 输出格式（严格）

{{"clusters": [
  {{"issue": "标题", "severity": "high", "message_ids": [1,5,12]}},
  {{"issue": "标题", "severity": "medium", "message_ids": [3,7]}}
]}}

## 消息列表（共 {n} 条）

{message_block}
"""


_PER_ISSUE_PROMPT_TPL = """你是行业舆情分析师。请就以下议题写一段**结构化深度分析**。

议题：{issue}
监控主题：{topic_name}（行业：{industry}）
监控方向：{monitor_direction}

## 输出格式（严格遵守，8 个子标题段 + 证据 blockquote）

```
**📍 现象**：（一句话事件描述，含时间/具体协议/产品/手段等关键事实）

**📊 规模**：（涉及哪几个频道/账号 + 用户量级 + 持续时间）

**💼 业务含义**：（对该主题监控的对象——产品体验、竞品格局、上游基建等——的具体影响判断）

**❓ 反例信号**：（是否有信息源说"没发生 / 影响被夸大 / 已恢复"？无则写"未发现反向证据"，30-60 字）

**🧪 什么会证伪**：（12 个月内什么数据/事件能证明这个 finding 是误判？30-60 字）

**⚠️ 误判风险**：（如果这个 finding 后来发现错了，最可能原因是？采样偏置 / 单源 / 时间窗口太短 / 用户表达放大 / 信源立场偏向 / 等，30-60 字）

**🔗 多源证实**：（是否被 ≥2 个独立信源证实？是 → 列信源名；否 → 标"单源风险，待补充验证"，30-60 字）

**📌 证据**：
> 内容片段 — 作者 · 频道 · 时间
> 内容片段 — 作者 · 频道 · 时间
```

## 硬性要求
- 不要写顶部 `## 标题` 或议题名（外层会拼）
- 不要写严重度 emoji（外层会拼）
- **8 个子标题**必须全部出现，缺一不可
- ❓ 反例 / 🧪 证伪 / ⚠️ 误判 三个反向校验字段必须主动找反向证据，不能简单写"无"
- 🔗 多源证实必须基于真实信源（看相关消息的频道字段），单源必须标"单源风险"
- 引用 2-3 条最具代表性原文
- 不编造未在消息中出现的事实
- 总长度 500-900 字（含反向校验字段）

## 相关消息（共 {n} 条）

{message_block}
"""


# ===================== 数据结构 =====================


@dataclass
class Issue:
    name: str
    severity: str       # 'high' | 'medium' | 'low'
    message_ids: list[int] = field(default_factory=list)
    body: str = ""      # per-issue LLM 输出的 8 字段段


@dataclass
class AnalysisData:
    topic: dict
    period_hours: int
    mode_used: str                      # 'single' | 'timeseries'
    message_count: int
    headline: str = ""
    single_body: str = ""               # mode='single' 时 LLM 直接输出
    issues: list[Issue] = field(default_factory=list)  # mode='timeseries' 时
    errors: list[str] = field(default_factory=list)

    @property
    def source_count(self) -> int:
        # 由调用方填充
        return getattr(self, "_source_count", 0)


# ===================== 辅助 =====================


def _format_message_block(messages: list[dict]) -> str:
    """messages → `[ID:N] [time] [source] author: content` 文本块。"""
    lines = []
    for m in messages:
        src = (m.get("source_display_name")
               or m.get("source_identifier")
               or m.get("source_kind", "?"))
        author = m.get("author") or "?"
        content = (m.get("content") or "").replace("\n", " ")[:500]
        posted_at = (m.get("posted_at") or "")[:16]
        lines.append(f"[ID:{m['id']}] [{posted_at}] [{src}] {author}: {content}")
    return "\n".join(lines)


def judge_mode(period_hours: int, message_count: int,
               user_choice: str = "auto") -> str:
    """选择模式 · auto 由数据规模自动定。"""
    if user_choice in ("single", "timeseries"):
        return user_choice
    # auto
    if period_hours <= 24 and message_count < 100:
        return "single"
    return "timeseries"


def _split_headline(text: str) -> tuple[str, str]:
    """从 LLM 输出第一行取标题（不带 #），剩余作 body。"""
    text = text.strip()
    lines = text.split("\n", 1)
    head = lines[0].strip().lstrip("#").strip()
    body = lines[1].strip() if len(lines) > 1 else ""
    return head, body


# ===================== LLM 调用封装 =====================


async def _call_llm(prompt: str, *, model: str, config: Config) -> str:
    """统一 LLM 调用（cli 或 api，按 config.llm.provider）· 失败返回空字符串。"""
    if config.llm.provider == "api":
        api_key = config.secrets.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.warning("provider=api 但缺 ANTHROPIC_API_KEY")
            return ""
        return await asyncio.to_thread(
            _call_api, prompt, model=model, api_key=api_key)
    return await asyncio.to_thread(
        _call_cli, prompt,
        model=model, cli_path=config.llm.cli_path,
        timeout=config.llm.cli_timeout)


# ===================== 主入口 =====================


async def build_analysis(topic: dict, messages: list[dict], *,
                         period_hours: int, mode: str,
                         config: Config) -> AnalysisData:
    """跑 LLM 分析，返回结构化 AnalysisData。

    mode_used 在 fallback 后可能与传入的 mode 不同。
    """
    mode_used = judge_mode(period_hours, len(messages), mode)
    log.info("analyze mode=%s (input=%s, messages=%d, period=%dh)",
             mode_used, mode, len(messages), period_hours)

    data = AnalysisData(
        topic=topic, period_hours=period_hours,
        mode_used=mode_used, message_count=len(messages),
    )
    # 统计 source 数
    src_set = set()
    for m in messages:
        src_set.add(m.get("source_id"))
    data._source_count = len(src_set)

    if not messages:
        data.headline = f"{topic['name']} · 过去 {period_hours} 小时无相关消息"
        return data

    model = config.models.deep_analysis

    if mode_used == "single":
        await _build_single(data, messages, model=model, config=config)
        return data

    # timeseries: cluster + per-issue
    cluster_ok = await _build_timeseries(data, messages, model=model, config=config)
    if not cluster_ok:
        # fallback：聚类失败 → 退到 single，仍保 8 字段（v2 关键改进）
        log.warning("cluster 失败 · fallback 到 single mode (仍含 8 字段)")
        data.mode_used = "single"
        data.errors.append("cluster_failed_fallback_to_single")
        await _build_single(data, messages, model=model, config=config)

    return data


async def _build_single(data: AnalysisData, messages: list[dict], *,
                       model: str, config: Config) -> None:
    prompt = _SINGLE_PROMPT_TPL.format(
        topic_name=data.topic["name"],
        industry=data.topic.get("industry", "general"),
        monitor_direction=data.topic.get("monitor_direction", ""),
        period_hours=data.period_hours,
        n=len(messages),
        message_block=_format_message_block(messages),
    )
    text = await _call_llm(prompt, model=model, config=config)
    if not text:
        data.errors.append("single_llm_failed")
        data.headline = f"{data.topic['name']} · 报告生成失败"
        data.single_body = "⚠ LLM 调用失败（cli timeout 或 API 错误），无报告生成。"
        return
    headline, body = _split_headline(text)
    data.headline = headline or f"{data.topic['name']} · 过去 {data.period_hours} 小时回顾"
    data.single_body = body


async def _build_timeseries(data: AnalysisData, messages: list[dict], *,
                           model: str, config: Config) -> bool:
    """returns True if 聚类 + per-issue 都成功，False 表示 cluster 失败需 fallback。"""
    # Step 1: cluster
    cluster_prompt = _CLUSTER_PROMPT_TPL.format(
        topic_name=data.topic["name"],
        industry=data.topic.get("industry", "general"),
        monitor_direction=data.topic.get("monitor_direction", ""),
        n=len(messages),
        message_block=_format_message_block(messages),
    )
    cluster_text = await _call_llm(cluster_prompt, model=model, config=config)
    cluster_obj = _extract_json(cluster_text)
    raw_clusters = cluster_obj.get("clusters") if cluster_obj else None
    if not raw_clusters:
        return False

    # Step 2: per-issue
    msgs_by_id = {m["id"]: m for m in messages}
    issues: list[Issue] = []
    for c in raw_clusters:
        if not isinstance(c, dict):
            continue
        name = str(c.get("issue", "")).strip()
        severity = str(c.get("severity", "medium")).strip().lower()
        if severity not in ("high", "medium", "low"):
            severity = "medium"
        msg_ids = [int(i) for i in c.get("message_ids", [])
                   if str(i).lstrip("-").isdigit()]
        related = [msgs_by_id[i] for i in msg_ids if i in msgs_by_id]
        if not name or not related:
            continue

        body_prompt = _PER_ISSUE_PROMPT_TPL.format(
            issue=name,
            topic_name=data.topic["name"],
            industry=data.topic.get("industry", "general"),
            monitor_direction=data.topic.get("monitor_direction", ""),
            n=len(related),
            message_block=_format_message_block(related),
        )
        body_text = await _call_llm(body_prompt, model=model, config=config)
        if not body_text:
            log.warning("per-issue LLM 失败: %s", name)
            data.errors.append(f"per_issue_failed:{name}")
            body_text = "⚠ 本议题 LLM 调用失败"

        issues.append(Issue(name=name, severity=severity,
                            message_ids=msg_ids, body=body_text.strip()))

    if not issues:
        return False

    data.issues = issues
    # headline 取 high → medium → low 第一个议题首句
    issues_sorted = sorted(
        issues,
        key=lambda i: {"high": 0, "medium": 1, "low": 2}.get(i.severity, 3),
    )
    first = issues_sorted[0]
    data.headline = f"{data.topic['name']} · {first.name}"
    return True
