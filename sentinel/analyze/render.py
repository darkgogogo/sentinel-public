"""analyze.render · 把 AnalysisData 组装成 markdown 写 KB。

aihot 原则 #2 落地点：**只关心呈现**，不拉数据不调 LLM。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from sentinel.analyze.data import AnalysisData
from sentinel.config import KB_ROOT


log = logging.getLogger(__name__)


REPORT_DIR_WEEKLY = KB_ROOT / "01-报告" / "周报"
REPORT_DIR_DEEP = KB_ROOT / "01-报告" / "主题深度报告"


SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _week_id(dt: datetime) -> str:
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def slugify_headline(headline: str, max_len: int = 40) -> str:
    """中文/英文混合 → 文件名安全 slug · 保留中文，转空格/特殊字符为 -。"""
    s = headline.strip()
    s = re.sub(r"[\s/\\:*?\"<>|]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "report"


def _frontmatter(tag: str, dt: datetime) -> str:
    return (
        "---\n"
        "tags:\n"
        f"  - {tag}\n"
        f"created: {dt.date().isoformat()}\n"
        "---\n\n"
    )


def _topic_slug(name: str) -> str:
    return re.sub(r"[\s/\\:*?\"<>|]+", "-", name.strip()).strip("-") or "topic"


def _resolve_path(data: AnalysisData, dt: datetime,
                 root_override: Path | None = None) -> Path:
    """周报 → weekly，否则深度报告。"""
    is_weekly = data.period_hours >= 168  # 7 天 = 168h
    if root_override:
        sub = "周报" if is_weekly else "主题深度报告"
        d = root_override / sub
    else:
        d = REPORT_DIR_WEEKLY if is_weekly else REPORT_DIR_DEEP
    d.mkdir(parents=True, exist_ok=True)

    if is_weekly:
        name = f"{_week_id(dt)}-{_topic_slug(data.topic['name'])}-周报.md"
    else:
        name = f"{dt.date().isoformat()}-{slugify_headline(data.headline)}.md"
    return d / name


def _tldr_from_issues(data: AnalysisData) -> str:
    """从 issues 首句拼一个粗糙 TL;DR（无额外 LLM call）。"""
    if not data.issues:
        return ""
    parts = []
    for issue in data.issues[:3]:
        # 取 body 第一行有内容的
        first = ""
        for ln in issue.body.split("\n"):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                first = ln[:60]
                break
        parts.append(f"{SEVERITY_EMOJI.get(issue.severity, '🟢')} {issue.name}: {first}")
    return " | ".join(parts)


def build_markdown(data: AnalysisData) -> str:
    """组装最终 markdown（不含 frontmatter，由调用方包装）。"""
    lines: list[str] = []
    lines.append(f"# {data.headline}")
    lines.append("")
    # 元信息
    lines.append(
        f"_主题: {data.topic['name']} · 行业: {data.topic.get('industry','?')} · "
        f"period={data.period_hours}h · mode={data.mode_used} · "
        f"messages={data.message_count} · sources={data.source_count}_"
    )
    lines.append("")
    if data.errors:
        lines.append(f"> ⚠ 部分 LLM 调用失败/降级: {', '.join(data.errors)}")
        lines.append("")

    if data.mode_used == "single":
        # LLM 输出的 single body 已含完整结构（速览 / 议题 / 行动建议 / TL;DR）
        lines.append(data.single_body)
        return "\n".join(lines)

    # timeseries 模式：本地拼装
    # 速览
    issues_sorted = sorted(
        data.issues,
        key=lambda i: {"high": 0, "medium": 1, "low": 2}.get(i.severity, 3),
    )
    lines.append("## 📊 本期速览")
    lines.append("")
    for issue in issues_sorted[:3]:
        emoji = SEVERITY_EMOJI.get(issue.severity, "🟢")
        # 议题 body 第一行通常是 📍 现象 起头
        first_phenomenon = ""
        for ln in issue.body.split("\n"):
            ln = ln.strip()
            if "📍" in ln:
                first_phenomenon = ln.split("：", 1)[-1].strip()[:80]
                break
        lines.append(f"> {emoji} **{issue.name}**：{first_phenomenon}")
    lines.append("")
    lines.append(
        f"📈 {data.message_count} 条消息 · {len(data.issues)} 个议题 · "
        f"横跨 {data.source_count} 个信源"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # 各议题
    for idx, issue in enumerate(issues_sorted, 1):
        emoji = SEVERITY_EMOJI.get(issue.severity, "🟢")
        lines.append(f"## {emoji} 议题 {idx} · {issue.name}")
        lines.append("")
        lines.append(issue.body.strip())
        lines.append("")
        lines.append("---")
        lines.append("")

    # TL;DR
    tldr = _tldr_from_issues(data)
    if tldr:
        lines.append("## 📝 TL;DR")
        lines.append("")
        lines.append(tldr)
        lines.append("")

    return "\n".join(lines)


def write_report_to_kb(data: AnalysisData,
                      root_override: Path | None = None) -> Path:
    """写 markdown 到 KB · 返回路径。

    周报：01-报告/周报/YYYY-W##-{topic}-周报.md
    深度：01-报告/主题深度报告/YYYY-MM-DD-{headline-slug}.md
    """
    now = datetime.now()
    path = _resolve_path(data, now, root_override)
    is_weekly = data.period_hours >= 168
    tag = "sentinel/report-weekly" if is_weekly else "sentinel/report-deep"
    md = _frontmatter(tag, now) + build_markdown(data)
    path.write_text(md, encoding="utf-8")
    log.info("KB 报告已写: %s", path)
    return path
