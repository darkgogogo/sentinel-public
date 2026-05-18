"""KB 写入 · 02-告警归档/YYYY-W##-告警合集.md 按周聚合 + 03-主题/[topic]/信源.md 维护。

写入根目录由 sentinel.config.KB_ROOT 决定（默认 ~/Documents/Sentinel，可通过
env SENTINEL_KB_ROOT 覆盖）。所有产出仅写入该根下的子目录，不外溢。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from sentinel.config import KB_ROOT
from sentinel.triage import TriageVerdict


log = logging.getLogger(__name__)


ALERT_ARCHIVE_DIR = KB_ROOT / "02-告警归档"
TOPIC_DIR = KB_ROOT / "03-主题"


def _week_id(dt: datetime) -> str:
    """`YYYY-W##` ISO week。"""
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _alert_archive_path(dt: datetime) -> Path:
    return ALERT_ARCHIVE_DIR / f"{_week_id(dt)}-告警合集.md"


def _frontmatter(tag: str, dt: datetime) -> str:
    return (
        "---\n"
        f"tags:\n"
        f"  - {tag}\n"
        f"created: {dt.date().isoformat()}\n"
        "---\n\n"
    )


def _group_evidence_by_source(messages: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for m in messages:
        src = (m.get("source_display_name")
               or m.get("source_identifier")
               or m.get("source_kind", "?"))
        grouped.setdefault(src, []).append(m)
    return grouped


def append_alert_archive(topic: dict, verdict: TriageVerdict,
                        messages: list[dict],
                        archive_root: Path | None = None) -> Path:
    """追加一条 alert 到当周聚合文件 · 返回文件路径。

    archive_root 用于测试覆盖；生产用默认 KB_ROOT/02-告警归档。
    """
    root = Path(archive_root) if archive_root else ALERT_ARCHIVE_DIR
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = (root / f"{_week_id(now)}-告警合集.md") if archive_root is None \
        else (root / f"{_week_id(now)}-告警合集.md")

    if not path.exists():
        path.write_text(
            _frontmatter("sentinel/alert-archive", now) +
            f"# {_week_id(now)} 告警合集\n\n"
            f"> 本周聚合 sentinel alert service 触发的全部告警，倒序排列。\n\n"
        )

    # 追加一条
    lines = []
    lines.append(f"## {now.strftime('%Y-%m-%d %H:%M')} · 🚨 {verdict.headline}")
    lines.append("")
    # summary as blockquote
    for ln in verdict.summary.split("\n"):
        lines.append(f"> {ln}")
    lines.append("")

    # 元信息表格
    lines.append("| 主题 | 行业 | 证据数 | 跨频道数 |")
    lines.append("|---|---|---|---|")
    related = set(verdict.related_message_ids)
    evidence_msgs = [m for m in messages if m["id"] in related] or messages
    grouped = _group_evidence_by_source(evidence_msgs)
    lines.append(
        f"| {topic.get('name','?')} | {topic.get('industry','?')} "
        f"| {len(evidence_msgs)} | {len(grouped)} |"
    )
    lines.append("")

    # 证据按频道分组
    for src, ms in grouped.items():
        lines.append(f"### 📍 {src}（{len(ms)} 条）")
        lines.append("")
        for m in ms[:5]:  # 每频道最多 5 条
            content = (m.get("content") or "").replace("\n", " ").strip()
            snippet = content[:200] + ("…" if len(content) > 200 else "")
            author = m.get("author") or "?"
            time_str = (m.get("posted_at") or "")[:16]
            url = m.get("url")
            line = f"- **{author}** · {time_str} — {snippet}"
            if url:
                line += f" [↗]({url})"
            lines.append(line)
        lines.append("")

    lines.append("---")
    lines.append("")

    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    log.info("KB 告警归档已写: %s", path)
    return path


def topic_slug(name: str) -> str:
    """topic name → 安全目录名（保留中文，转空格 / 特殊字符为 -）。"""
    import re
    s = name.strip()
    s = re.sub(r"[\s/\\:*?\"<>|]+", "-", s)
    return s.strip("-") or "topic"


def topic_sources_path(topic_name: str) -> Path:
    return TOPIC_DIR / topic_slug(topic_name) / "信源.md"


def update_advisor_section(topic: dict, sources: list[dict],
                          section_md: str,
                          topic_root: Path | None = None) -> Path:
    """覆盖式更新 03-主题/[topic]/信源.md 的 `## advisor 建议` section。

    其他段（元信息 / 信源清单 / 用户自定义段）**完全保留**。
    如果文件不存在，先调 ensure_topic_sources_file 初始化。
    """
    p = ensure_topic_sources_file(topic, sources, topic_root)
    text = p.read_text(encoding="utf-8")

    # 找 `## advisor 建议` 起始 + 下一个 `^## ` 结束
    lines = text.split("\n")
    start_idx = None
    end_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == "## advisor 建议":
            start_idx = i
            continue
        if start_idx is not None and ln.startswith("## ") and i > start_idx:
            end_idx = i
            break

    new_section_lines = section_md.split("\n")
    if start_idx is not None:
        # 替换现有 advisor section
        if end_idx is None:
            end_idx = len(lines)
        new_lines = lines[:start_idx] + new_section_lines + [""] + lines[end_idx:]
    else:
        # 追加到文件末尾
        new_lines = lines + [""] + new_section_lines + [""]

    p.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    log.info("advisor section 已更新: %s", p)
    return p


def ensure_topic_sources_file(topic: dict, sources: list[dict],
                             topic_root: Path | None = None) -> Path:
    """确保 03-主题/[slug]/信源.md 存在并初始化（如果不存在）。

    返回路径。如已存在不覆盖（advisor 后续会更新 advisor section）。
    """
    root = Path(topic_root) if topic_root else TOPIC_DIR
    p = root / topic_slug(topic["name"]) / "信源.md"
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists():
        return p

    now = datetime.now()
    fm = (
        "---\n"
        "tags:\n"
        "  - sentinel/topic-sources\n"
        f"topic: {topic['name']}\n"
        f"industry: {topic.get('industry', '?')}\n"
        f"created: {now.date().isoformat()}\n"
        "---\n\n"
    )

    lines = [
        fm,
        f"# {topic['name']} · 信源记录\n",
        "## 元信息",
        f"- industry: {topic.get('industry', '?')}",
        f"- alert_enabled: {bool(topic.get('alert_enabled', 1))}",
        f"- 创建: {now.date().isoformat()}",
        f"- monitor_direction: {topic.get('monitor_direction', '')}",
        "",
        "## 信源清单（sources）",
    ]
    for s in sources:
        kind = s.get("kind", "?")
        ident = s.get("identifier", "?")
        display = s.get("display_name") or ident
        lines.append(f"- {kind} · `{ident}`（{display}）")
    lines.append("")
    lines.append("## advisor 建议")
    lines.append("")
    lines.append("> 暂无 · advisor service 跑过一次后会自动填充")
    lines.append("")

    p.write_text("\n".join(lines), encoding="utf-8")
    log.info("KB 主题信源文件已建: %s", p)
    return p
