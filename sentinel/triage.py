"""Triage · 调 Haiku 4.5 判定 messages 是否值得告警。

设计：
- v1.1 三关 gate（具体新事件 + 跨源 ≥2 + 业务相关性）+ 例外通道 + 二次收紧（防群聊接龙误报）沿用
- v2 通用化：prompt 用 topic.industry / monitor_direction 参数化，不锁死 VPN/letsvpn
- LLM 调用：cli (subprocess claude) + api (anthropic SDK) 双路，由 config.llm.provider 决定
- 失败 graceful：LLM 调用超时/返回非 JSON → 返回 worth_alert=False，不抛
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from sentinel.config import Config


log = logging.getLogger(__name__)


PROMPT_TEMPLATE = """你是行业舆情监控助手。请判定一批消息是否包含"会影响 {industry} 行业格局、{topic_name} 主题相关方业务"的信号。

监控主题：{topic_name}
所属行业：{industry}
监控方向：{monitor_direction}
关键词：{keywords}

过去时段相关消息（共 {n} 条）：
{message_block}

## 触发告警的硬性条件（任一不满足 → worth_alert=false）

1. **具体新事件 + 事件本身 fresh**（不是日常吐槽 / 不是现状回顾 / 不是旧事件余波）：
   该消息是否描述一个**具体发生的新事件**——有明确的时间锚点（"今早 8 点起" / "昨天" / "5/11"）+ 具体表现 + 可观察规模（"大面积" / "多家" / "全线"）+ **事件本身距今 ≤ 7 天**？
   - ❌ 单纯吐槽（"我这卡了" / "节点慢"）→ 否
   - ❌ 回顾性现状评论（"这波惨的是 X" / "最近一段时间 Y 多"）→ 否，这是对过去趋势的总结，不是新事件
   - ❌ 历史反复现象（无具体新表现的"又出问题了"）→ 否
   - ❌ **旧事件余波讨论**：用户在讨论已发生事件的后续/复盘/退款到账/影响延伸（"前几天才到账" / "终于退了" / "已经卸载 app 了" / "为啥还没退我" / "之前的 X 现在怎样" / "我也收到退款"），即使消息本身是新发的、事件本身仍是旧的 → 否。**判别提示**：过去式 + 个人后续行为 + 无新增事实，几乎一定是余波而非新事件。
   - ✅ 真正新颖事件（带具体时间 + 具体细节 + 规模佐证 + 事件本身 7 天内）→ 是

2. **跨源验证**：是否被 ≥2 个**独立频道**同时讨论同一具体事件？
   - **关键定义**：这里的"频道"严格按消息块里 `[方括号源名]` 维度算，**不是**按发言人算。同一频道里 N 个不同用户对同一话题附和 → **仍算单源**，不满足跨源
   - ❌ 单频道讨论（哪怕群里 5 个人接龙）→ 否
   - ✅ 多个不同 `[方括号源名]` 独立提到同一具体事件 → 是
   - **重大事件例外**：单源独家但属"全行业冲击级事件"（独家曝光重大政策变化 / 头部产品官方公告大事 / 基础设施大规模事件）→ 仍可告警，但 headline 必须以"【独家】"开头便于人工复核

3. **业务相关性**：对该 topic 监控的对象（用户体验 / 竞品格局 / 行业上游）有具体影响？
   - ❌ 纯八卦 / 无规模反馈 → 否
   - ✅ 直接影响（明确指向该主题关注的对象）→ 是

## 不要触发告警的反例（务必识别）

- ❌ 单一用户的零星抱怨
- ❌ **群内多人接龙吐槽同一感受**（不是跨源，是单源讨论）
- ❌ **行业现状回顾**（"这波惨的是 X" 类总结性评论，不是新事件）
- ❌ **旧事件余波讨论**（用户聊"前几天才到账" / "已经退款了" / "终于退了" / "为啥还没退我" 等，是 N 天 / N 周前事件的后续；事件本身已不 fresh，即使消息很新）
- ❌ 日常推荐 / 比较 / 教程 / 求助
- ❌ 无时间锚点的 vague 抱怨
- ❌ 已经在过去 30 天报过的同类事件（除非有质变升级）

## 判定流程

先在心里走 1+2+3 三关，全部 yes 才输出 worth_alert=true。多条消息描述同一事件 → 合并成一个告警。

只输出 JSON：
{{
  "worth_alert": true/false,
  "headline": "20 字内一句概括（必须包含具体事件，不要泛泛）",
  "summary": "100 字内简短描述：发生了什么 + 影响范围 + 证据来源",
  "related_message_ids": [1, 5, 12]
}}
"""


@dataclass
class TriageVerdict:
    worth_alert: bool = False
    headline: str = ""
    summary: str = ""
    related_message_ids: list[int] = field(default_factory=list)
    raw_response: str = ""  # debug

    @property
    def is_valid(self) -> bool:
        if not self.worth_alert:
            return True  # 不告警也是有效结论
        return bool(self.headline and self.summary)


def _format_message_block(messages: list[dict]) -> str:
    """messages → LLM prompt 里的 `[ID:N] [time] [source] author: content` 段。"""
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


def _extract_json(text: str) -> dict:
    """从 LLM 输出里提 JSON · 容忍 ```json fence + 数组形式 + 前后噪音。

    LLM 偶尔会输出数组 `[{...}]` 而不是单 dict（即使 prompt 说"输出 JSON"），
    这里做兼容：数组取第一个 worth_alert=true 的元素；没 true 取第一个 dict。
    """
    if not text:
        return {}
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    obj: Any = None
    try:
        obj = json.loads(text)
    except Exception:
        # 找第一个 {...} 或 [...] 块
        for pat in (r"\[.*\]", r"\{.*\}"):
            m = re.search(pat, text, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group())
                    break
                except Exception:
                    continue

    if obj is None:
        return {}

    if isinstance(obj, list):
        if not obj:
            return {}
        for item in obj:
            if isinstance(item, dict) and item.get("worth_alert"):
                return item
        first = obj[0]
        return first if isinstance(first, dict) else {}

    return obj if isinstance(obj, dict) else {}


def _call_cli(prompt: str, *, model: str, cli_path: str, timeout: int) -> str:
    """调本地 claude CLI · 复用 Pro 订阅。返回模型 text（已剥 JSON 包装），失败返回空字符串。

    成功调用时把 usage（input/output tokens + cost）经 llm_usage.record_call 上报给
    ServiceShell 当前 run 的累加器。
    """
    from sentinel.runtime.llm_usage import record_call
    try:
        proc = subprocess.run(
            [cli_path, "-p", "--output-format", "json", "--model", model, prompt],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.warning("claude CLI 调用失败: %s", e)
        return ""
    if proc.returncode != 0:
        log.warning("claude CLI returncode=%d, stderr=%s",
                    proc.returncode, proc.stderr[:200])
        return ""
    try:
        outer = json.loads(proc.stdout)
        usage = outer.get("usage") or {}
        if usage or "total_cost_usd" in outer:
            record_call({
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
                "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0)),
                "cost_usd": float(outer.get("total_cost_usd", 0.0)),
            })
        return outer.get("result", "")
    except Exception:
        return proc.stdout


def _call_api(prompt: str, *, model: str, api_key: str) -> str:
    """调 Anthropic SDK · 需要 ANTHROPIC_API_KEY。失败返回空字符串。

    成功调用时上报 usage（API mode 无 cost_usd，需算价计费；先记 token，cost=0）。
    """
    from sentinel.runtime.llm_usage import record_call
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        if msg.usage is not None:
            record_call({
                "input_tokens": int(getattr(msg.usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(msg.usage, "output_tokens", 0) or 0),
                "cache_read_input_tokens": int(
                    getattr(msg.usage, "cache_read_input_tokens", 0) or 0),
                "cache_creation_input_tokens": int(
                    getattr(msg.usage, "cache_creation_input_tokens", 0) or 0),
                "cost_usd": 0.0,  # API mode 不直接给 cost，后续按 model 计价
            })
        return "".join(b.text for b in msg.content if hasattr(b, "text"))
    except Exception as e:
        log.warning("anthropic API 调用失败: %s", e)
        return ""


async def judge(topic: dict, messages: list[dict], *,
                config: Config) -> TriageVerdict:
    """给定 topic + 一组 messages，返回 TriageVerdict。

    LLM 调用是同步 IO（subprocess / SDK），用 asyncio.to_thread 跑。
    """
    if not messages:
        return TriageVerdict(worth_alert=False)

    keywords = topic.get("_keywords", []) or []
    prompt = PROMPT_TEMPLATE.format(
        industry=topic.get("industry", "general"),
        topic_name=topic["name"],
        monitor_direction=topic.get("monitor_direction", ""),
        keywords=", ".join(keywords) if keywords else "(未设置)",
        n=len(messages),
        message_block=_format_message_block(messages),
    )

    model = config.models.triage
    if config.llm.provider == "api":
        api_key = config.secrets.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.warning("provider=api 但缺 ANTHROPIC_API_KEY")
            return TriageVerdict(worth_alert=False)
        text = await asyncio.to_thread(_call_api, prompt, model=model, api_key=api_key)
    else:
        text = await asyncio.to_thread(
            _call_cli, prompt,
            model=model, cli_path=config.llm.cli_path,
            timeout=config.llm.cli_timeout)

    data = _extract_json(text)
    if not data:
        log.warning("triage LLM 返回非 JSON · text=%r", text[:200])
        return TriageVerdict(worth_alert=False, raw_response=text)

    return TriageVerdict(
        worth_alert=bool(data.get("worth_alert", False)),
        headline=str(data.get("headline", "")).strip(),
        summary=str(data.get("summary", "")).strip(),
        related_message_ids=[int(i) for i in data.get("related_message_ids", []) if str(i).lstrip("-").isdigit()],
        raw_response=text,
    )
