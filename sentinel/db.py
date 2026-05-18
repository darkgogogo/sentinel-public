"""Sentinel v2 SQLite schema + 基础 CRUD。

v2 关键差异（vs v1）：
- topics 加 `industry`（行业分组）+ `alert_enabled`（告警开关）字段
- daemon_runs → service_runs（加 `service` 字段，标 collect/alert/analyze/advisor）
- 不实体化 industries 表（通过 SELECT DISTINCT industry FROM topics 派生）
- collector kind 是内部实现细节，schema 上仍是 sources.kind 字符串
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import aiosqlite


# 归一化 headline 用于去重 · 抓"中美关税升级" vs "中美贸易关税升级"
_PUNCT_RE = re.compile(
    r"[\s\.\,\!\?\:\;\(\)\[\]\{\}\"\'\-—–_/\\\|"
    r"。，！？：；（）「」《》【】、…"
    r"]+"
)


def _normalize_headline(s: str) -> str:
    return _PUNCT_RE.sub("", (s or "").lower())


HEADLINE_SIMILARITY_THRESHOLD = 0.75


def _headlines_similar(a_norm: str, b_norm: str) -> bool:
    """判定两 headline（已归一化）是否构成 dedup."""
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    # 双向 substring（"中美关税升级" ⊂ "今日中美关税升级公告"）
    if a_norm in b_norm or b_norm in a_norm:
        return True
    return SequenceMatcher(None, a_norm, b_norm).ratio() >= HEADLINE_SIMILARITY_THRESHOLD


SCHEMA_SQL = """
-- 数据源（统一抽象，所有 collector kind 都用同一张表）
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                    -- 'telegram' | 'rss' | 'twitter' | 'reddit' | 'social' | ...
    identifier TEXT NOT NULL,              -- 各 kind 自定义（@channel / url / handle / platform-name）
    display_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',-- kind 特定参数
    failure_count INTEGER NOT NULL DEFAULT 0,  -- 连续失败计数（达上限自动 disable）
    created_at TEXT NOT NULL,
    UNIQUE(kind, identifier)
);

-- 监控主题（v2 新增 industry + alert_enabled 字段）
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    industry TEXT NOT NULL DEFAULT 'general',  -- 行业分组标签（不实体化）
    monitor_direction TEXT NOT NULL,           -- 自然语言，给 LLM 看
    alert_enabled INTEGER NOT NULL DEFAULT 1,  -- 主题级告警开关
    weekly_enabled INTEGER NOT NULL DEFAULT 1, -- 周报开关（默认随 alert）
    enabled INTEGER NOT NULL DEFAULT 1,
    backfill_hours INTEGER NOT NULL DEFAULT 0, -- 新增 topic 后首次 alert 时回看的窗口，跑完清 0
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_keywords (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_sources (
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    skip_keyword_filter INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (topic_id, source_id)
);

-- 原始消息
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    author TEXT,
    content TEXT NOT NULL,
    url TEXT,
    posted_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    raw_json TEXT,
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_posted_at ON messages(posted_at);
CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source_id);

-- 告警历史
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER REFERENCES topics(id),
    triggered_at TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    related_message_ids TEXT,              -- JSON array
    push_status TEXT,                      -- 'sent' | 'failed' | 'pending'
    push_channels TEXT,                    -- JSON: ['telegram','email']
    pushed_at TEXT,
    user_label TEXT,                       -- 'true_positive' | 'false_positive' | NULL
    user_label_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_triggered_at ON alerts(triggered_at);

-- service 运行日志（v2 重命名自 daemon_runs，加 service 字段）
CREATE TABLE IF NOT EXISTS service_runs (
    id INTEGER PRIMARY KEY,
    service TEXT NOT NULL,                 -- 'collect' | 'alert' | 'analyze' | 'advisor'
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,                  -- 'running' | 'success' | 'failed' | 'skipped'
    lookback_hours REAL,
    messages_collected INTEGER DEFAULT 0,
    alerts_triggered INTEGER DEFAULT 0,
    error TEXT,
    -- v2.1 LLM 成本字段
    llm_tokens_in INTEGER NOT NULL DEFAULT 0,
    llm_tokens_out INTEGER NOT NULL DEFAULT 0,
    llm_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    llm_cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    llm_cost_usd REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_runs_service_status ON service_runs(service, status);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON service_runs(started_at);

-- v2.2 覆盖审计 (每周一 09:00 由 watchdog 跑一次，每个 topic 算覆盖健康度)
CREATE TABLE IF NOT EXISTS topic_coverage_audit (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    audited_at TEXT NOT NULL,
    severity TEXT NOT NULL,                -- 'high' (push) | 'medium' (banner) | 'ok'
    metrics_json TEXT NOT NULL,            -- {msgs_7d, platform_count, failure_pct, signal_ratio}
    diagnosis_md TEXT,                     -- LLM 给的可执行建议 markdown
    rsshub_suggestions_json TEXT,          -- [{url, display_name, confidence}, ...]
    webaccess_suggestions_json TEXT,       -- [{platform, keyword, reason}, ...]
    status TEXT NOT NULL DEFAULT 'pending',-- pending / executed / dismissed
    dismissed_until TEXT,                  -- ISO datetime; NULL = 无 dismissal
    executed_at TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_cov_audit_topic ON topic_coverage_audit(topic_id, audited_at DESC);
CREATE INDEX IF NOT EXISTS idx_cov_audit_status ON topic_coverage_audit(status, severity);
"""


# 旧 db 升级到 v2.1 时补列（CREATE TABLE IF NOT EXISTS 不会重建表）。
_MIGRATIONS_V21_LLM_COST = [
    "ALTER TABLE service_runs ADD COLUMN llm_tokens_in INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE service_runs ADD COLUMN llm_tokens_out INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE service_runs ADD COLUMN llm_cache_read_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE service_runs ADD COLUMN llm_cache_creation_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE service_runs ADD COLUMN llm_cost_usd REAL NOT NULL DEFAULT 0.0",
]

_MIGRATIONS_V21_TOPIC_BACKFILL = [
    "ALTER TABLE topics ADD COLUMN backfill_hours INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE topics ADD COLUMN weekly_enabled INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE topics ADD COLUMN coverage_threshold_overrides TEXT",
    "ALTER TABLE topics ADD COLUMN alert_interval_hours INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE topics ADD COLUMN last_alert_checked_at TEXT",
]

# §19 增量 (2026-05-18): topic_sources 加 alert_enabled —— 让"用户讨论区"类 source
# 仅供 analyze/advisor 用，不参与 alert triage（同一 source 不同 topic 可独立配置）
_MIGRATIONS_V22_TOPIC_SOURCES_ALERT_FLAG = [
    "ALTER TABLE topic_sources ADD COLUMN alert_enabled INTEGER NOT NULL DEFAULT 1",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """async SQLite 封装（WAL 模式）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init_schema(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript("PRAGMA journal_mode=WAL;")
            await db.executescript(SCHEMA_SQL)
            await self._apply_migrations(db, "service_runs",
                                         _MIGRATIONS_V21_LLM_COST)
            await self._apply_migrations(db, "topics",
                                         _MIGRATIONS_V21_TOPIC_BACKFILL)
            await self._apply_migrations(db, "topic_sources",
                                         _MIGRATIONS_V22_TOPIC_SOURCES_ALERT_FLAG)
            await db.commit()

    @staticmethod
    async def _apply_migrations(db: aiosqlite.Connection, table: str,
                                stmts: list[str]) -> None:
        """sqlite ALTER 不支持 IF NOT EXISTS — 查 pragma table_info 后增量执行。"""
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            cols = {row[1] async for row in cur}
        for stmt in stmts:
            col_name = stmt.split("ADD COLUMN ")[1].split(" ")[0]
            if col_name not in cols:
                await db.execute(stmt)

    # ---------- sources ----------

    async def list_enabled_sources(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sources WHERE enabled=1 ORDER BY id"
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def insert_source(self, kind: str, identifier: str,
                            display_name: str | None = None,
                            config: dict | None = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO sources (kind, identifier, display_name, config_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (kind, identifier, display_name,
                 json.dumps(config or {}), _now_iso()),
            )
            await db.commit()
            return cur.lastrowid or 0

    async def disable_source(self, source_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sources SET enabled=0 WHERE id=?", (source_id,))
            await db.commit()

    async def increment_failure(self, source_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sources SET failure_count = failure_count + 1 WHERE id=?",
                (source_id,))
            await db.commit()
            async with db.execute(
                "SELECT failure_count FROM sources WHERE id=?", (source_id,)) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0

    async def reset_failure(self, source_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sources SET failure_count=0 WHERE id=?", (source_id,))
            await db.commit()

    # ---------- messages ----------

    async def message_exists(self, source_id: int, external_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM messages WHERE source_id=? AND external_id=? LIMIT 1",
                (source_id, external_id)) as cur:
                return await cur.fetchone() is not None

    async def insert_message(self, source_id: int, external_id: str,
                             author: str | None, content: str,
                             url: str | None, posted_at: datetime,
                             raw_json: dict | None = None) -> int | None:
        """插入 message，重复（UNIQUE 冲突）返回 None。"""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                cur = await db.execute(
                    """INSERT INTO messages
                       (source_id, external_id, author, content, url,
                        posted_at, collected_at, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source_id, external_id, author, content, url,
                     posted_at.isoformat(), _now_iso(),
                     json.dumps(raw_json or {}, default=str)),
                )
                await db.commit()
                return cur.lastrowid
            except aiosqlite.IntegrityError:
                return None

    async def purge_old_messages(self, retention_days: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM messages WHERE posted_at < datetime('now', ?)",
                (f"-{retention_days} days",))
            await db.commit()
            return cur.rowcount

    async def purge_old_service_runs(self, retention_days: int) -> int:
        """清 service_runs 表的旧记录（按 started_at）。alerts 表永不清。"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM service_runs WHERE started_at < datetime('now', ?)",
                (f"-{retention_days} days",))
            await db.commit()
            return cur.rowcount

    # ---------- service_runs ----------

    async def insert_run(self, service: str, lookback_hours: float | None = None,
                         status: str = "running",
                         error: str | None = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO service_runs (service, started_at, status, lookback_hours, error)
                   VALUES (?, ?, ?, ?, ?)""",
                (service, _now_iso(), status, lookback_hours, error),
            )
            await db.commit()
            return cur.lastrowid or 0

    async def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "finished_at" not in fields and fields.get("status") in (
                "success", "failed", "skipped"):
            fields["finished_at"] = _now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [run_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE service_runs SET {cols} WHERE id=?", vals)
            await db.commit()

    # ---------- topic_coverage_audit ----------

    async def insert_coverage_audit(self, *, topic_id: int, severity: str,
                                    metrics: dict,
                                    diagnosis_md: str | None = None,
                                    rsshub_suggestions: list | None = None,
                                    webaccess_suggestions: list | None = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO topic_coverage_audit
                   (topic_id, audited_at, severity, metrics_json,
                    diagnosis_md, rsshub_suggestions_json, webaccess_suggestions_json,
                    status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (topic_id, _now_iso(), severity,
                 json.dumps(metrics, ensure_ascii=False),
                 diagnosis_md,
                 json.dumps(rsshub_suggestions or [], ensure_ascii=False),
                 json.dumps(webaccess_suggestions or [], ensure_ascii=False)))
            await db.commit()
            return cur.lastrowid or 0

    async def latest_coverage_audit(self, topic_id: int) -> dict | None:
        """该 topic 最近一次 audit（不限 status）。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM topic_coverage_audit WHERE topic_id=?
                   ORDER BY audited_at DESC LIMIT 1""", (topic_id,)) as cur:
                row = await cur.fetchone()
            return dict(row) if row else None

    async def pending_coverage_audits(self) -> list[dict]:
        """所有 status=pending 且 (dismissed_until IS NULL OR dismissed_until < now) 的 audit。
        Banner / push 用。joined topic name。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT a.*, t.name AS topic_name, t.industry AS topic_industry
                   FROM topic_coverage_audit a
                   JOIN topics t ON t.id = a.topic_id
                   WHERE a.status = 'pending'
                     AND (a.dismissed_until IS NULL
                          OR a.dismissed_until < datetime('now'))
                   ORDER BY
                     CASE a.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                     a.audited_at DESC""") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def dismiss_coverage_audit(self, audit_id: int,
                                     dismissed_until: str | None) -> None:
        """dismissed_until = ISO datetime / None=永久（status=dismissed 且无 until）。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE topic_coverage_audit
                   SET status='dismissed', dismissed_until=?
                   WHERE id=?""", (dismissed_until, audit_id))
            await db.commit()

    async def execute_coverage_audit(self, audit_id: int,
                                     notes: str | None = None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE topic_coverage_audit
                   SET status='executed', executed_at=?, notes=?
                   WHERE id=?""", (_now_iso(), notes, audit_id))
            await db.commit()

    async def append_run_error(self, run_id: int, msg: str) -> None:
        """追加 error 字段（保留原有内容 + 分号分隔），不改 status。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT error FROM service_runs WHERE id=?", (run_id,)) as cur:
                row = await cur.fetchone()
            existing = (row["error"] if row and row["error"] else "")
            merged = (existing + " ; " + msg) if existing else msg
            await db.execute(
                "UPDATE service_runs SET error=? WHERE id=?", (merged, run_id))
            await db.commit()

    async def last_successful_run(self, service: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM service_runs
                   WHERE service=? AND status='success'
                   ORDER BY started_at DESC LIMIT 1""",
                (service,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    # ---------- topics + topic_sources + topic_keywords ----------

    async def insert_topic(self, name: str, industry: str,
                           monitor_direction: str,
                           alert_enabled: bool = True,
                           weekly_enabled: bool | None = None,
                           backfill_hours: int = 0,
                           alert_interval_hours: int = 0) -> int:
        if weekly_enabled is None:
            weekly_enabled = alert_enabled  # 默认随 alert
        async with aiosqlite.connect(self.db_path) as db:
            now = _now_iso()
            cur = await db.execute(
                """INSERT INTO topics
                   (name, industry, monitor_direction, alert_enabled,
                    weekly_enabled, enabled, backfill_hours,
                    alert_interval_hours, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (name, industry, monitor_direction,
                 1 if alert_enabled else 0,
                 1 if weekly_enabled else 0,
                 max(0, int(backfill_hours)),
                 max(0, int(alert_interval_hours)), now, now),
            )
            await db.commit()
            return cur.lastrowid or 0

    async def reset_topic_backfill(self, topic_id: int) -> None:
        """alert 处理过 backfill_hours>0 的 topic 后调，清零防重复消费。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE topics SET backfill_hours=0, updated_at=? WHERE id=?",
                (_now_iso(), topic_id))
            await db.commit()

    async def update_topic(self, topic_id: int, *,
                           name: str | None = None,
                           industry: str | None = None,
                           monitor_direction: str | None = None,
                           backfill_hours: int | None = None,
                           alert_interval_hours: int | None = None) -> None:
        """更新 topic 字段（None 表示不改）。"""
        fields: dict[str, Any] = {}
        if name is not None: fields["name"] = name.strip()
        if industry is not None: fields["industry"] = industry.strip() or "general"
        if monitor_direction is not None: fields["monitor_direction"] = monitor_direction.strip()
        if backfill_hours is not None: fields["backfill_hours"] = max(0, int(backfill_hours))
        if alert_interval_hours is not None:
            fields["alert_interval_hours"] = max(0, int(alert_interval_hours))
        if not fields:
            return
        fields["updated_at"] = _now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [topic_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE topics SET {cols} WHERE id=?", vals)
            await db.commit()

    async def touch_topic_alert_checked(self, topic_id: int) -> None:
        """alert service 处理过 topic 后调（无论是否产 alert），更新 last_alert_checked_at。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE topics SET last_alert_checked_at=? WHERE id=?",
                (_now_iso(), topic_id))
            await db.commit()

    async def update_source(self, source_id: int, *,
                            identifier: str | None = None,
                            display_name: str | None = None) -> None:
        """更新 source 字段（None 表示不改）。"""
        fields: dict[str, Any] = {}
        if identifier is not None: fields["identifier"] = identifier.strip()
        if display_name is not None:
            d = display_name.strip()
            fields["display_name"] = d if d else None
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [source_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE sources SET {cols} WHERE id=?", vals)
            await db.commit()

    async def list_alerting_topics(self) -> list[dict]:
        """alert service 用：只看 alert_enabled 的 topics。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM topics WHERE enabled=1 AND alert_enabled=1 "
                "ORDER BY id") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def list_weekly_topics(self) -> list[dict]:
        """analyze weekly 用：weekly_enabled topic（独立于 alert_enabled）。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM topics WHERE enabled=1 AND weekly_enabled=1 "
                "ORDER BY id") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def toggle_topic_weekly(self, topic_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE topics SET weekly_enabled = 1 - weekly_enabled, "
                "updated_at=? WHERE id=?", (_now_iso(), topic_id))
            await db.commit()

    async def list_all_topics(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM topics ORDER BY industry, id") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def add_topic_keyword(self, topic_id: int, keyword: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            # 简单去重
            async with db.execute(
                "SELECT 1 FROM topic_keywords WHERE topic_id=? AND keyword=?",
                (topic_id, keyword)) as cur:
                if await cur.fetchone():
                    return
            await db.execute(
                "INSERT INTO topic_keywords (topic_id, keyword) VALUES (?, ?)",
                (topic_id, keyword))
            await db.commit()

    async def list_topic_keywords(self, topic_id: int) -> list[str]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT keyword FROM topic_keywords WHERE topic_id=?",
                (topic_id,)) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def link_topic_source(self, topic_id: int, source_id: int,
                                skip_keyword_filter: bool = False,
                                alert_enabled: bool = True) -> None:
        """topic 关联 source · 幂等。

        alert_enabled=False: source 仍参与 analyze/advisor，但不进 alert triage
        （适用于"用户讨论区"类源，回声多事件少，会持续触发旧事件余波误报）。
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO topic_sources
                   (topic_id, source_id, skip_keyword_filter, alert_enabled)
                   VALUES (?, ?, ?, ?)""",
                (topic_id, source_id,
                 1 if skip_keyword_filter else 0,
                 1 if alert_enabled else 0))
            await db.commit()

    async def list_topic_sources(self, topic_id: int,
                                 alert_only: bool = False) -> list[dict]:
        """topic 关联的 sources (JOIN，含 skip_keyword_filter / alert_enabled)。

        alert_only=True: 只返回 alert_enabled=1 的源（alert service 用）。
        默认 False: 返回全部（analyze/advisor/coverage_audit 用）。
        """
        sql = """SELECT s.*, ts.skip_keyword_filter, ts.alert_enabled
                 FROM sources s JOIN topic_sources ts ON ts.source_id=s.id
                 WHERE ts.topic_id=? AND s.enabled=1"""
        if alert_only:
            sql += " AND ts.alert_enabled=1"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (topic_id,)) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ---------- 消息查询（alert / analyze 用）----------

    async def list_messages_since(self, source_ids: list[int],
                                  since: datetime) -> list[dict]:
        """指定 sources 范围内、since 之后的消息（含 source 元信息）。"""
        if not source_ids:
            return []
        placeholders = ",".join("?" * len(source_ids))
        sql = (
            f"SELECT m.*, s.kind AS source_kind, s.identifier AS source_identifier, "
            f"       s.display_name AS source_display_name "
            f"FROM messages m JOIN sources s ON s.id=m.source_id "
            f"WHERE m.source_id IN ({placeholders}) AND m.posted_at >= ? "
            f"ORDER BY m.posted_at DESC"
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, [*source_ids, since.isoformat()]) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ---------- alerts ----------

    async def insert_alert(self, topic_id: int, headline: str, summary: str,
                           related_message_ids: list[int],
                           push_status: str = "pending",
                           push_channels: list[str] | None = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO alerts
                   (topic_id, triggered_at, headline, summary,
                    related_message_ids, push_status, push_channels)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (topic_id, _now_iso(), headline, summary,
                 json.dumps(related_message_ids),
                 push_status,
                 json.dumps(push_channels or [])),
            )
            await db.commit()
            return cur.lastrowid or 0

    async def update_alert_push_status(self, alert_id: int, status: str,
                                       channels: list[str] | None = None) -> None:
        fields: dict[str, Any] = {"push_status": status}
        if status == "sent":
            fields["pushed_at"] = _now_iso()
        if channels is not None:
            fields["push_channels"] = json.dumps(channels)
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [alert_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE alerts SET {cols} WHERE id=?", vals)
            await db.commit()

    async def alert_exists_recently(self, topic_id: int, headline: str,
                                    hours: int = 24) -> bool:
        """24h 同 topic 去重检查 · 归一化 + SequenceMatcher 模糊匹配。

        改自精确字符串匹配。归一化策略：lowercase + 去中英标点空白。
        相似度 >= 0.85 视为重复（抓"中美关税升级" vs "中美贸易关税升级"等同义改写）。
        """
        target = _normalize_headline(headline)
        if not target:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """SELECT headline FROM alerts
                   WHERE topic_id=?
                     AND triggered_at >= datetime('now', ?)
                     AND push_status='sent'""",
                (topic_id, f"-{hours} hours")) as cur:
                rows = await cur.fetchall()
        for (existing,) in rows:
            if _headlines_similar(_normalize_headline(existing or ""), target):
                return True
        return False

    async def recent_alerts(self, limit: int = 20,
                            topic_id: int | None = None,
                            push_status: str | None = None,
                            user_label: str | None = None) -> list[dict]:
        clauses = []
        params: list[Any] = []
        if topic_id is not None:
            clauses.append("a.topic_id = ?")
            params.append(topic_id)
        if push_status is not None:
            clauses.append("a.push_status = ?")
            params.append(push_status)
        if user_label is not None:
            if user_label == "_unlabeled":
                clauses.append("a.user_label IS NULL")
            else:
                clauses.append("a.user_label = ?")
                params.append(user_label)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT a.*, t.name AS topic_name, t.industry AS topic_industry
                    FROM alerts a LEFT JOIN topics t ON t.id=a.topic_id
                    {where}
                    ORDER BY a.triggered_at DESC LIMIT ?""",
                params + [limit]) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_messages_by_ids(self, message_ids: list[int]) -> list[dict]:
        """按 id 列表查 messages（用于 alert detail 展开 related）。"""
        if not message_ids:
            return []
        placeholders = ",".join("?" * len(message_ids))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT m.*, s.kind AS source_kind, s.display_name AS source_display
                    FROM messages m LEFT JOIN sources s ON s.id=m.source_id
                    WHERE m.id IN ({placeholders})
                    ORDER BY m.posted_at DESC""",
                message_ids) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def label_alert(self, alert_id: int, label: str | None) -> None:
        """标记 alert 为 true_positive / false_positive / 清除（None）。"""
        if label not in (None, "true_positive", "false_positive"):
            raise ValueError(f"invalid label: {label!r}")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE alerts SET user_label=?, user_label_at=? WHERE id=?",
                (label, _now_iso() if label else None, alert_id))
            await db.commit()

    # ---------- 通用删除/更新（D2 写入）----------

    async def list_all_sources(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sources ORDER BY kind, id") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_source(self, source_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sources WHERE id=?", (source_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_topic(self, topic_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM topics WHERE id=?", (topic_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def enable_source(self, source_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sources SET enabled=1, failure_count=0 WHERE id=?",
                (source_id,))
            await db.commit()

    async def toggle_topic_alert(self, topic_id: int) -> bool:
        """切换 alert_enabled。返回新状态。"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT alert_enabled FROM topics WHERE id=?",
                (topic_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return False
                new_val = 0 if row[0] else 1
            await db.execute(
                "UPDATE topics SET alert_enabled=?, updated_at=? WHERE id=?",
                (new_val, _now_iso(), topic_id))
            await db.commit()
            return bool(new_val)

    async def delete_topic(self, topic_id: int) -> None:
        """删 topic + 级联（topic_keywords / topic_sources / topic_coverage_audit 由 FK CASCADE）。
        alerts.topic_id 没有 CASCADE（历史 alert 要保留），先置 NULL 再删 topic。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("UPDATE alerts SET topic_id=NULL WHERE topic_id=?", (topic_id,))
            await db.execute("DELETE FROM topics WHERE id=?", (topic_id,))
            await db.commit()

    async def unlink_topic_source(self, topic_id: int, source_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM topic_sources WHERE topic_id=? AND source_id=?",
                (topic_id, source_id))
            await db.commit()

    async def remove_topic_keyword(self, topic_id: int, keyword: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM topic_keywords WHERE topic_id=? AND keyword=?",
                (topic_id, keyword))
            await db.commit()

    async def delete_source(self, source_id: int) -> None:
        """硬删 source + 历史 messages · topic_sources 由 FK CASCADE。
        messages.source_id NOT NULL 故不能 SET NULL，等同于丢失该 source 的全部历史数据。"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("DELETE FROM messages WHERE source_id=?", (source_id,))
            await db.execute("DELETE FROM sources WHERE id=?", (source_id,))
            await db.commit()

    # ---------- advisor 用查询 ----------

    async def messages_count_per_source(self, source_ids: list[int],
                                        since: datetime) -> dict[int, int]:
        """每个 source 在 since 之后的消息数。"""
        if not source_ids:
            return {}
        placeholders = ",".join("?" * len(source_ids))
        sql = (
            f"SELECT source_id, COUNT(*) FROM messages "
            f"WHERE source_id IN ({placeholders}) AND posted_at >= ? "
            f"GROUP BY source_id"
        )
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, [*source_ids, since.isoformat()]) as cur:
                return {row[0]: row[1] for row in await cur.fetchall()}

    async def alerts_for_topic(self, topic_id: int,
                              since: datetime) -> list[dict]:
        """该 topic 在 since 之后的 alerts（含 related_message_ids JSON 字符串）。

        调用方需 json.loads(row['related_message_ids']) 拿到 list[int]。
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM alerts
                   WHERE topic_id=? AND triggered_at >= ?
                   ORDER BY triggered_at DESC""",
                (topic_id, since.isoformat())) as cur:
                return [dict(r) for r in await cur.fetchall()]
