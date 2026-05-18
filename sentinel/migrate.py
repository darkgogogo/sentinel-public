"""v1 → v2 信源 / 主题配置迁移。

从 v1 sentinel SQLite 读 的 sources / topics / topic_keywords /
topic_sources，写入当前 v2 db。

行为：
- 只迁移**配置**（信源/主题/关键词/关联），不迁移 messages / alerts / runs
- v1 没有 industry / alert_enabled / failure_count，用 v2 默认值（industry='VPN'，
  alert_enabled=1, failure_count=0）
- UNIQUE 冲突跳过（kind+identifier 重复的 source、name 重复的 topic）
- 默认 dry-run，--apply 才实际写入
- 输出迁移报告

支持的 source kind（v1 → v2）：
  - telegram → telegram
  - rss → rss
  - reddit → reddit（v1.1 决策走 RSS 端点）
  - twitter → twitter
  其他未知 kind 跳过 + 告警
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from sentinel.db import Database


log = logging.getLogger(__name__)

DEFAULT_V1_DB = Path.home() / "sentinel-legacy" / "db" / "messages.db"  # v1 默认路径; 用户可 --v1-db 覆盖

# v1 没有 industry / alert_enabled 字段，全部默认 VPN 行业、启用告警
DEFAULT_INDUSTRY = "VPN"
DEFAULT_ALERT_ENABLED = True


@dataclass
class MigrateReport:
    sources_added: int = 0
    sources_skipped_exists: int = 0
    sources_skipped_unknown_kind: int = 0
    topics_added: int = 0
    topics_skipped_exists: int = 0
    keywords_added: int = 0
    links_added: int = 0
    links_skipped_missing: int = 0
    kb_stubs_created: int = 0
    kb_stubs_existing: int = 0
    unknown_kinds: list[str] = field(default_factory=list)
    sources_added_detail: list[tuple[str, str, str]] = field(default_factory=list)
    topics_added_detail: list[tuple[str, str]] = field(default_factory=list)
    dry_run: bool = True


def _connect_v1(v1_db: Path) -> sqlite3.Connection:
    if not v1_db.exists():
        raise FileNotFoundError(f"v1 db 不存在: {v1_db}")
    conn = sqlite3.connect(str(v1_db))
    conn.row_factory = sqlite3.Row
    return conn


def _read_v1_sources(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT id, kind, identifier, display_name, enabled, config_json "
        "FROM sources WHERE enabled=1")
    return [dict(r) for r in cur.fetchall()]


def _read_v1_topics(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT id, name, monitor_direction FROM topics WHERE enabled=1")
    return [dict(r) for r in cur.fetchall()]


def _read_v1_keywords(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT topic_id, keyword FROM topic_keywords")
    return [dict(r) for r in cur.fetchall()]


def _read_v1_topic_sources(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT topic_id, source_id, skip_keyword_filter FROM topic_sources")
    return [dict(r) for r in cur.fetchall()]


async def migrate(v1_db: Path, db: Database, *,
                 dry_run: bool = True) -> MigrateReport:
    """主迁移流程。Returns report。"""
    report = MigrateReport(dry_run=dry_run)

    v1 = _connect_v1(v1_db)
    try:
        v1_sources = _read_v1_sources(v1)
        v1_topics = _read_v1_topics(v1)
        v1_keywords = _read_v1_keywords(v1)
        v1_topic_sources = _read_v1_topic_sources(v1)
    finally:
        v1.close()

    # 当前 v2 状态
    v2_sources = await db.list_all_sources()
    v2_topics = await db.list_all_topics()
    existing_source_keys = {(s["kind"], s["identifier"]) for s in v2_sources}
    existing_topic_names = {t["name"] for t in v2_topics}

    from sentinel.collectors import KIND_REGISTRY, auto_discover
    auto_discover()
    known_kinds = set(KIND_REGISTRY.keys())

    # v1 source id → v2 source id（用于后续 topic_sources 转换）
    v1_to_v2_source_id: dict[int, int] = {}
    # v1 topic id → v2 topic id
    v1_to_v2_topic_id: dict[int, int] = {}

    # 1. 迁移 sources
    for s in v1_sources:
        kind = s["kind"]
        if kind not in known_kinds:
            report.sources_skipped_unknown_kind += 1
            if kind not in report.unknown_kinds:
                report.unknown_kinds.append(kind)
            log.warning("source #%s kind=%s 未知，跳过", s["id"], kind)
            continue
        key = (kind, s["identifier"])
        if key in existing_source_keys:
            report.sources_skipped_exists += 1
            # 找到对应 v2 source id
            for v2s in v2_sources:
                if (v2s["kind"], v2s["identifier"]) == key:
                    v1_to_v2_source_id[s["id"]] = v2s["id"]
                    break
            continue
        report.sources_added += 1
        report.sources_added_detail.append(
            (kind, s["identifier"], s.get("display_name") or ""))
        if not dry_run:
            try:
                config = json.loads(s.get("config_json") or "{}")
            except json.JSONDecodeError:
                config = {}
            new_id = await db.insert_source(
                kind=kind, identifier=s["identifier"],
                display_name=s.get("display_name"),
                config=config)
            v1_to_v2_source_id[s["id"]] = new_id
        else:
            # dry-run 占位，让后续 keyword/link 计数能继续
            v1_to_v2_source_id[s["id"]] = -1

    # 2. 迁移 topics
    for t in v1_topics:
        if t["name"] in existing_topic_names:
            report.topics_skipped_exists += 1
            for v2t in v2_topics:
                if v2t["name"] == t["name"]:
                    v1_to_v2_topic_id[t["id"]] = v2t["id"]
                    break
            continue
        report.topics_added += 1
        report.topics_added_detail.append((t["name"], t["monitor_direction"]))
        if not dry_run:
            new_id = await db.insert_topic(
                name=t["name"],
                industry=DEFAULT_INDUSTRY,
                monitor_direction=t["monitor_direction"],
                alert_enabled=DEFAULT_ALERT_ENABLED)
            v1_to_v2_topic_id[t["id"]] = new_id
        else:
            v1_to_v2_topic_id[t["id"]] = -1

    # 3. 迁移 keywords (只有 topic 真的进 v2 才迁)
    for k in v1_keywords:
        v2_tid = v1_to_v2_topic_id.get(k["topic_id"])
        if v2_tid is None:
            continue
        if not dry_run:
            await db.add_topic_keyword(v2_tid, k["keyword"])
        report.keywords_added += 1

    # 4. 迁移 topic_sources 关联
    for ts in v1_topic_sources:
        v2_tid = v1_to_v2_topic_id.get(ts["topic_id"])
        v2_sid = v1_to_v2_source_id.get(ts["source_id"])
        if v2_tid is None or v2_sid is None:
            report.links_skipped_missing += 1
            continue
        if not dry_run:
            await db.link_topic_source(
                v2_tid, v2_sid,
                skip_keyword_filter=bool(ts.get("skip_keyword_filter")))
        report.links_added += 1

    # 5. 为每个 v2 topic 初始化 03-主题/[slug]/信源.md stub
    # （只在 apply 模式跑；ensure_topic_sources_file 已有就不覆盖）
    if not dry_run:
        from sentinel.kb import ensure_topic_sources_file, topic_sources_path
        v2_topics_after = await db.list_all_topics()
        for t in v2_topics_after:
            srcs = await db.list_topic_sources(t["id"])
            existed_before = topic_sources_path(t["name"]).exists()
            ensure_topic_sources_file(t, srcs)
            if existed_before:
                report.kb_stubs_existing += 1
            else:
                report.kb_stubs_created += 1

    return report


def format_report(r: MigrateReport) -> str:
    lines = []
    lines.append(f"=== migrate-v1 报告 ({'DRY-RUN' if r.dry_run else 'APPLIED'}) ===")
    lines.append("")
    lines.append(f"  sources:")
    lines.append(f"    + {r.sources_added} 新加")
    lines.append(f"    - {r.sources_skipped_exists} 跳过（已存在）")
    lines.append(f"    - {r.sources_skipped_unknown_kind} 跳过（未知 kind: "
                 f"{', '.join(r.unknown_kinds) if r.unknown_kinds else '—'})")
    lines.append("")
    lines.append(f"  topics:")
    lines.append(f"    + {r.topics_added} 新加")
    lines.append(f"    - {r.topics_skipped_exists} 跳过（已存在）")
    lines.append(f"  keywords + {r.keywords_added}")
    lines.append(f"  topic↔source links: + {r.links_added}, skip {r.links_skipped_missing}")
    if not r.dry_run:
        lines.append(f"  KB 信源.md stub: + {r.kb_stubs_created} 新建, "
                     f"= {r.kb_stubs_existing} 已存在")
    if r.dry_run and r.sources_added_detail:
        lines.append("")
        lines.append("  即将新加的 sources (前 10):")
        for s in r.sources_added_detail[:10]:
            lines.append(f"    - [{s[0]}] {s[1]}  ({s[2]})")
        if len(r.sources_added_detail) > 10:
            lines.append(f"    ... 还有 {len(r.sources_added_detail) - 10} 个")
    if r.dry_run and r.topics_added_detail:
        lines.append("")
        lines.append("  即将新加的 topics:")
        for t in r.topics_added_detail:
            lines.append(f"    - {t[0]}  ({t[1][:50]}...)")
    if r.dry_run:
        lines.append("")
        lines.append("→ 这是 dry-run · 加 --apply 实际执行")
    return "\n".join(lines)


# ============== CLI handler ==============


async def run_migrate(v1_db_str: str | None, apply: bool) -> int:
    from sentinel.config import DB_PATH
    v1_db = Path(v1_db_str) if v1_db_str else DEFAULT_V1_DB
    db = Database(str(DB_PATH))
    await db.init_schema()
    report = await migrate(v1_db, db, dry_run=not apply)
    print(format_report(report))
    return 0
