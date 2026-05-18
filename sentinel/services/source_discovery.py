"""Source discovery · AI 根据主题描述推荐新信源。

跟 sentinel/services/advisor.py（评估现有信源的信噪比）不同：
- advisor: 已有信源 → 评分 + 建议加/减权重
- discovery: 主题描述 → 用 web search 推荐新信源（之前不存在的）

输出 candidates 列表，前端展示给用户勾选，提交时一并创建对应 sources 记录。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from sentinel.config import Config
from sentinel.triage import _call_api, _call_cli


log = logging.getLogger(__name__)


SUPPORTED_KINDS = {"rss", "telegram", "twitter", "reddit"}

PROMPT_TEMPLATE = """你是一个信源研究助手，根据用户的主题描述，使用 web search 工具搜索互联网，推荐适合订阅的真实信源。

主题信息：
- 名称：{name}
- 行业：{industry}
- 监控方向：{monitor_direction}

请用 web search 找出 5-10 个**真实存在且活跃**的信源。

支持的 kind：
- rss: 完整 RSS feed URL（如 https://www.solidot.org/index.rss）— 最优先
- reddit: subreddit 名称（如 selfhosted；不带 r/）
- telegram: Telegram 公开频道（@channel_name 格式）
- twitter: Twitter/X handle（如 elonmusk；不带 @）

要求：
1. **优先 RSS**（最稳定）；信源类型越多越好（避免全是单一类型）
2. identifier **严格**按上述格式
3. **不要编造** URL — 只推荐你通过 web search 验证存在的
4. 中英文都覆盖（如果该主题相关）
5. confidence 字段：high = 这个 source 高度相关且活跃；medium = 相关但可能不活跃；low = 边缘相关

按相关度排序。输出 JSON 数组，**仅 JSON 不要其他任何文本**：

[
  {{"kind": "rss", "identifier": "https://www.example.com/rss", "display_name": "Example Tech Blog", "reason": "覆盖你监控方向中的 X 议题，更新频繁", "confidence": "high"}},
  {{"kind": "reddit", "identifier": "selfhosted", "display_name": "r/selfhosted", "reason": "...", "confidence": "medium"}}
]
"""


def _extract_json_list(text: str) -> list:
    """从 LLM 返回中提取 JSON 数组。容错处理 markdown code fence。"""
    if not text:
        return []
    text = text.strip()
    # 剥 ``` json ``` / ``` ``` fence
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    # 找第一个 [ ... ] 块
    bracket_match = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if bracket_match:
        text = bracket_match.group(0)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, list) else []
    except json.JSONDecodeError:
        return []


def _normalize_candidate(raw: dict) -> dict | None:
    """validate + sanitize 单个候选。返回 None 表示丢弃。"""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind", "")).strip().lower()
    if kind not in SUPPORTED_KINDS:
        return None
    identifier = str(raw.get("identifier", "")).strip()
    if not identifier or len(identifier) > 500:
        return None

    # per-kind 格式快速校验
    if kind == "rss" and not identifier.lower().startswith(("http://", "https://")):
        return None
    if kind == "telegram":
        identifier = identifier.lstrip("@")
        if not identifier:
            return None
    if kind == "twitter":
        identifier = identifier.lstrip("@")
        identifier = identifier.split("/")[-1]  # url 末段
        if not identifier:
            return None
    if kind == "reddit":
        identifier = identifier.lstrip("r/").lstrip("/")
        if not identifier:
            return None

    confidence = str(raw.get("confidence", "medium")).strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"

    display_name = str(raw.get("display_name", "")).strip()[:200]
    reason = str(raw.get("reason", "")).strip()[:300]

    return {
        "kind": kind,
        "identifier": identifier,
        "display_name": display_name,
        "reason": reason,
        "confidence": confidence,
    }


async def discover_sources(*, name: str, industry: str,
                           monitor_direction: str,
                           config: Config) -> list[dict]:
    """让 LLM 用 web search 推荐信源。返回 candidates 列表。"""
    prompt = PROMPT_TEMPLATE.format(
        name=name.strip() or "?",
        industry=industry.strip() or "general",
        monitor_direction=monitor_direction.strip() or "(未提供)",
    )

    # 用 deep_analysis 模型（Opus / Sonnet），需要 web search 能力
    model = config.models.deep_analysis
    if config.llm.provider == "api":
        api_key = config.secrets.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.warning("provider=api 但缺 ANTHROPIC_API_KEY")
            return []
        text = await asyncio.to_thread(
            _call_api, prompt, model=model, api_key=api_key)
    else:
        text = await asyncio.to_thread(
            _call_cli, prompt,
            model=model, cli_path=config.llm.cli_path,
            timeout=config.llm.cli_timeout)

    if not text:
        log.warning("discover_sources: LLM returned empty")
        return []

    raw_list = _extract_json_list(text)
    if not raw_list:
        log.warning("discover_sources: 非 JSON 数组输出: %r", text[:200])
        return []

    candidates: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for item in raw_list:
        norm = _normalize_candidate(item)
        if not norm:
            continue
        key = (norm["kind"], norm["identifier"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(norm)
    return candidates
