"""routes · 6 个只读 endpoint。

/         dashboard 主页（综合）
/alerts   告警列表
/reports  报告列表（KB 文件 + 在线查看）
/topics   主题列表（按 industry 分组）
/sources  信源列表（按 kind 分组）
/status   service_runs 历史
"""
from __future__ import annotations

import re
from pathlib import Path
from datetime import datetime
from typing import Any

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from fastapi import (
    FastAPI, Request, HTTPException, Form, BackgroundTasks,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from markdown_it import MarkdownIt

from sentinel.collectors import KIND_REGISTRY, auto_discover
from sentinel.config import KB_ROOT, PROJECT_ROOT
from sentinel.runtime.shell import PAUSE_FILE_PREFIX


log = logging.getLogger(__name__)


REPORTS_DIR = KB_ROOT / "01-报告"
ARCHIVE_DIR = KB_ROOT / "02-告警归档"

# Markdown 渲染器（用于 reports 在线查看）
_md = MarkdownIt("commonmark", {"breaks": True, "html": False})


def _safe_kb_path(rel: str) -> Path:
    """防 path traversal · 只允许 KB 内文件。"""
    target = (KB_ROOT / rel).resolve()
    if not str(target).startswith(str(KB_ROOT.resolve())):
        raise HTTPException(400, "path 越界")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return target


def _service_pause_status() -> dict[str, dict]:
    """各 service 的 pause 状态（基于 .pause-<service> 文件）。"""
    out = {}
    for svc in ("collect", "alert", "analyze", "advisor"):
        f = PROJECT_ROOT / f"{PAUSE_FILE_PREFIX}{svc}"
        out[svc] = {"paused": f.exists()}
    return out


def _toggle_badge_html(on: bool, name: str) -> str:
    """htmx 局部返回：单个 toggle badge。"""
    if on:
        return f'<span class="badge ok">on</span>'
    return f'<span class="badge cold">off</span>'


def _next_hour_tick(hours: list[int], minute: int = 0) -> str:
    """根据 launchd StartCalendarInterval 算下一个触发本地时间。"""
    now = datetime.now()
    today_ticks = [
        now.replace(hour=h, minute=minute, second=0, microsecond=0)
        for h in hours
    ]
    future = [t for t in today_ticks if t > now]
    next_tick = future[0] if future else (today_ticks[0] + timedelta(days=1))
    return next_tick.strftime("%m-%d %H:%M")


async def _service_status_summary(db_path: str) -> dict[str, Any]:
    """各 service 最近 1 次成功 run。"""
    out = {}
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        for svc in ("collect", "alert", "analyze", "advisor"):
            async with db.execute(
                """SELECT * FROM service_runs
                   WHERE service=? AND status='success'
                   ORDER BY started_at DESC LIMIT 1""", (svc,)) as cur:
                row = await cur.fetchone()
                out[svc] = dict(row) if row else None
    return out


async def _count_simple(db_path: str, table: str, where: str = "") -> int:
    async with aiosqlite.connect(db_path) as db:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        async with db.execute(sql) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


def _extract_first_paragraph(path: Path, max_chars: int = 200) -> str:
    """读 markdown 文件，剥 frontmatter，返回第一段非空文字（去 #）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            text = text[end + 5:]
    for para in text.split("\n\n"):
        cleaned = "\n".join(
            ln.lstrip("# ").strip()
            for ln in para.split("\n") if ln.strip()
        ).strip()
        if cleaned and not cleaned.startswith("```"):
            return cleaned[:max_chars]
    return ""


def _list_kb_reports() -> list[dict]:
    """扫 01-报告/ 下所有 .md，返回 [{path, type, name, mtime}]。"""
    out = []
    for sub in ("周报", "主题深度报告"):
        d = REPORTS_DIR / sub
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime,
                       reverse=True):
            out.append({
                "rel": str(p.relative_to(KB_ROOT)),
                "type": sub,
                "name": p.stem,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime),
            })
    return out


def _list_kb_archives() -> list[dict]:
    """扫 02-告警归档/ 下所有 .md。"""
    out = []
    if not ARCHIVE_DIR.exists():
        return out
    for p in sorted(ARCHIVE_DIR.glob("*.md"),
                   key=lambda x: x.stat().st_mtime, reverse=True):
        out.append({
            "rel": str(p.relative_to(KB_ROOT)),
            "name": p.stem,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime),
        })
    return out


def register_routes(app: FastAPI) -> None:
    templates = app.state.templates

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        db = app.state.db
        await db.init_schema()
        db_path = db.db_path

        sources = await db.list_enabled_sources()
        topics = await db.list_all_topics()
        recent_alerts = await db.recent_alerts(limit=5)
        # 给 dashboard 的最近告警也拉 1-3 related preview（仅 summary 用，dashboard 不展开）
        recent_reports = _list_kb_reports()[:5]
        recent_archives = _list_kb_archives()[:3]
        svc_status = await _service_status_summary(db_path)
        svc_pause = _service_pause_status()
        alert_count = await _count_simple(db_path, "alerts")
        msg_count = await _count_simple(db_path, "messages")

        # 待处理 coverage audit（用户没 dismiss / 未过期）
        pending_audits = await db.pending_coverage_audits()
        high_audits = [a for a in pending_audits if a["severity"] == "high"]
        medium_audits = [a for a in pending_audits if a["severity"] == "medium"]

        # 快失活信源（failure_count >= limit-2 且 enabled）
        config = app.state.config
        failure_limit = config.services.collect.source_failure_limit
        struggling_threshold = max(1, failure_limit - 2)
        async with aiosqlite.connect(db_path) as raw:
            raw.row_factory = aiosqlite.Row
            async with raw.execute(
                "SELECT * FROM sources WHERE enabled=1 AND failure_count >= ? "
                "ORDER BY failure_count DESC, id",
                (struggling_threshold,)) as cur:
                struggling_sources = [dict(r) for r in await cur.fetchall()]

            # 24h 新增 messages + alerts
            async with raw.execute(
                "SELECT COUNT(*) FROM messages WHERE collected_at >= datetime('now', '-24 hours')"
            ) as cur:
                msgs_24h = (await cur.fetchone())[0]
            async with raw.execute(
                "SELECT COUNT(*) FROM alerts WHERE triggered_at >= datetime('now', '-24 hours')"
            ) as cur:
                alerts_24h = (await cur.fetchone())[0]
            async with raw.execute(
                "SELECT COUNT(*) FROM alerts WHERE triggered_at >= datetime('now', '-24 hours') AND push_status='sent'"
            ) as cur:
                alerts_24h_sent = (await cur.fetchone())[0]

            # 30 天 LLM 总成本（dashboard glance）
            async with raw.execute(
                "SELECT COALESCE(SUM(llm_cost_usd), 0.0) FROM service_runs WHERE started_at >= datetime('now', '-30 days')"
            ) as cur:
                cost_30d = float((await cur.fetchone())[0])

            # 进行中的 service runs（status=running）
            async with raw.execute(
                """SELECT service, started_at FROM service_runs
                   WHERE status='running' AND finished_at IS NULL
                   ORDER BY started_at DESC""") as cur:
                running_now = [dict(r) for r in await cur.fetchall()]

            # 过去 7 天每天 messages 入库量（mini trend）
            async with raw.execute(
                """SELECT date(collected_at) AS d, COUNT(*) AS c
                   FROM messages
                   WHERE collected_at >= datetime('now', '-7 days')
                   GROUP BY date(collected_at)
                   ORDER BY d""") as cur:
                trend_rows = [dict(r) for r in await cur.fetchall()]

        # 7 天 trend 补齐缺日（无消息那天补 0）
        from datetime import date as _date
        today_local = datetime.now().date()
        days_full = [(today_local - timedelta(days=i)) for i in range(6, -1, -1)]
        trend_dict = {r["d"]: r["c"] for r in trend_rows}
        msg_trend = [(d.strftime("%m-%d"), trend_dict.get(d.isoformat(), 0)) for d in days_full]
        trend_max = max((c for _, c in msg_trend), default=0) or 1

        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "sources": sources,
            "topics": topics,
            "recent_alerts": recent_alerts,
            "recent_reports": recent_reports,
            "recent_archives": recent_archives,
            "svc_status": svc_status,
            "svc_pause": svc_pause,
            "alert_count": alert_count,
            "msg_count": msg_count,
            "msgs_24h": msgs_24h,
            "alerts_24h": alerts_24h,
            "alerts_24h_sent": alerts_24h_sent,
            "cost_30d": cost_30d,
            "running_now": running_now,
            "msg_trend": msg_trend,
            "trend_max": trend_max,
            "struggling_sources": struggling_sources,
            "failure_limit": failure_limit,
            "high_audits": high_audits,
            "medium_audits": medium_audits,
        })

    @app.get("/alerts", response_class=HTMLResponse)
    async def alerts_page(request: Request, limit: int = 100,
                         topic_id: int | None = None,
                         push_status: str | None = None,
                         user_label: str | None = None):
        db = app.state.db
        await db.init_schema()
        alerts = await db.recent_alerts(
            limit=limit, topic_id=topic_id,
            push_status=push_status, user_label=user_label)
        # 给每条 alert 拉关联 messages（最多 5 条 preview）
        import json
        for a in alerts:
            rmids_raw = a.get("related_message_ids") or "[]"
            try:
                ids = json.loads(rmids_raw) if isinstance(rmids_raw, str) else rmids_raw
            except Exception:
                ids = []
            a["_related_messages"] = await db.get_messages_by_ids(ids[:5])
        # filter UI 用：所有 topic
        all_topics = await db.list_all_topics()
        return templates.TemplateResponse(request=request, name="alerts.html", context={
            "alerts": alerts,
            "limit": limit,
            "filter_topic_id": topic_id,
            "filter_push_status": push_status,
            "filter_user_label": user_label,
            "all_topics": all_topics,
        })

    @app.get("/reports", response_class=HTMLResponse)
    async def reports_page(request: Request,
                          type_filter: str | None = None,
                          q: str | None = None):
        reports = _list_kb_reports()
        archives = _list_kb_archives()
        # 给每个 report 加 summary（读 markdown 文件首段）
        for r in reports:
            r["_summary"] = _extract_first_paragraph(KB_ROOT / r["rel"])
        # 过滤
        if type_filter:
            reports = [r for r in reports if r.get("type") == type_filter]
        if q:
            ql = q.lower()
            reports = [r for r in reports if ql in r["name"].lower() or
                       ql in (r.get("_summary") or "").lower()]
        return templates.TemplateResponse(request=request, name="reports.html", context={
            "reports": reports,
            "archives": archives,
            "type_filter": type_filter,
            "q": q,
        })

    @app.get("/reports/view", response_class=HTMLResponse)
    async def report_view(request: Request, rel: str):
        path = _safe_kb_path(rel)
        text = path.read_text(encoding="utf-8")
        # 剥 frontmatter
        body = text
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end > 0:
                body = text[end + 5:]
        html = _md.render(body)
        # 上下篇导航（同目录、同 type）
        all_reports = _list_kb_reports()
        prev_rel = next_rel = None
        prev_name = next_name = None
        for i, r in enumerate(all_reports):
            if r["rel"] == rel:
                if i > 0:
                    next_rel = all_reports[i-1]["rel"]
                    next_name = all_reports[i-1]["name"]
                if i + 1 < len(all_reports):
                    prev_rel = all_reports[i+1]["rel"]
                    prev_name = all_reports[i+1]["name"]
                break
        # 报告类型 + 时间元信息
        rtype = "?"
        if "/周报/" in rel:
            rtype = "周报"
        elif "/主题深度报告/" in rel:
            rtype = "主题深度报告"
        return templates.TemplateResponse(request=request, name="report_view.html", context={
            "rel": rel,
            "name": path.stem,
            "html": html,
            "raw": text,
            "rtype": rtype,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime),
            "prev_rel": prev_rel,
            "prev_name": prev_name,
            "next_rel": next_rel,
            "next_name": next_name,
        })

    @app.get("/topics", response_class=HTMLResponse)
    async def topics_page(request: Request):
        db = app.state.db
        await db.init_schema()
        topics = await db.list_all_topics()
        # 给每个 topic 取 source 数 + keyword 数
        for t in topics:
            t["_sources"] = await db.list_topic_sources(t["id"])
            t["_keywords"] = await db.list_topic_keywords(t["id"])
        # 按 industry 分组
        by_industry: dict[str, list[dict]] = {}
        for t in topics:
            by_industry.setdefault(t.get("industry", "general"), []).append(t)
        return templates.TemplateResponse(request=request, name="topics.html", context={
            "by_industry": by_industry,
        })

    @app.get("/sources", response_class=HTMLResponse)
    async def sources_page(request: Request,
                          industry: str | None = None,
                          topic_id: int | None = None):
        db = app.state.db
        await db.init_schema()
        # 构造 SQL：按 industry / topic_id 过滤（through topic_sources）
        clauses, params = ["1=1"], []
        if topic_id is not None:
            clauses.append("EXISTS (SELECT 1 FROM topic_sources ts WHERE ts.source_id=s.id AND ts.topic_id=?)")
            params.append(topic_id)
        if industry:
            clauses.append(
                "EXISTS (SELECT 1 FROM topic_sources ts JOIN topics t ON t.id=ts.topic_id "
                "WHERE ts.source_id=s.id AND t.industry=?)")
            params.append(industry)
        where = " AND ".join(clauses)
        async with aiosqlite.connect(db.db_path) as raw_db:
            raw_db.row_factory = aiosqlite.Row
            async with raw_db.execute(
                f"SELECT s.* FROM sources s WHERE {where} "
                f"ORDER BY s.failure_count DESC, s.kind, s.id",
                params) as cur:
                sources = [dict(r) for r in await cur.fetchall()]
            # 每个 source 关联的 topic + 历史 message 数（用于显示 + delete confirm）
            for s in sources:
                async with raw_db.execute(
                    "SELECT t.id, t.name, t.industry "
                    "FROM topic_sources ts JOIN topics t ON t.id=ts.topic_id "
                    "WHERE ts.source_id=? ORDER BY t.industry, t.name",
                    (s["id"],)) as cur:
                    s["_topics"] = [dict(r) for r in await cur.fetchall()]
                async with raw_db.execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE source_id=?",
                    (s["id"],)) as cur:
                    row = await cur.fetchone()
                    s["message_count"] = row["c"] if row else 0
            # filter UI 用：所有 industry + 所有 topic
            async with raw_db.execute(
                "SELECT DISTINCT industry FROM topics WHERE enabled=1 "
                "ORDER BY industry") as cur:
                industries = [r["industry"] for r in await cur.fetchall()]
            async with raw_db.execute(
                "SELECT id, name, industry FROM topics WHERE enabled=1 "
                "ORDER BY industry, name") as cur:
                all_topics_flat = [dict(r) for r in await cur.fetchall()]
        struggling = [s for s in sources if s.get("failure_count", 0) > 0]
        healthy = [s for s in sources if s.get("failure_count", 0) == 0]
        by_kind: dict[str, list[dict]] = {}
        for s in healthy:
            by_kind.setdefault(s["kind"], []).append(s)
        auto_discover()
        return templates.TemplateResponse(request=request, name="sources.html", context={
            "struggling": struggling,
            "by_kind": by_kind,
            "kinds": sorted(KIND_REGISTRY.keys()),
            "industries": industries,
            "all_topics_flat": all_topics_flat,
            "filter_industry": industry,
            "filter_topic_id": topic_id,
            "total_count": len(sources),
        })

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request, limit: int = 50):
        db = app.state.db
        await db.init_schema()
        async with aiosqlite.connect(db.db_path) as raw_db:
            raw_db.row_factory = aiosqlite.Row
            async with raw_db.execute(
                "SELECT * FROM service_runs ORDER BY started_at DESC LIMIT ?",
                (limit,)) as cur:
                runs = [dict(r) for r in await cur.fetchall()]
            # 各 service 最近一次成功 run 时间（service 控制行用）
            last_success: dict[str, str] = {}
            async with raw_db.execute(
                """SELECT service, MAX(started_at) AS ts FROM service_runs
                   WHERE status='success' GROUP BY service""") as cur:
                async for row in cur:
                    last_success[row["service"]] = row["ts"]
            # 当前在跑的 service（status=running，无 finished_at）
            async with raw_db.execute(
                """SELECT service, started_at FROM service_runs
                   WHERE status='running' AND finished_at IS NULL
                   ORDER BY started_at DESC""") as cur:
                running_now = [dict(r) for r in await cur.fetchall()]
            # 最近 30 天 LLM 成本聚合（按 service 分）
            async with raw_db.execute(
                """SELECT service,
                          COUNT(*) AS runs,
                          COALESCE(SUM(llm_tokens_in), 0) AS tokens_in,
                          COALESCE(SUM(llm_tokens_out), 0) AS tokens_out,
                          COALESCE(SUM(llm_cost_usd), 0.0) AS cost_usd
                   FROM service_runs
                   WHERE started_at >= datetime('now', '-30 days')
                     AND llm_cost_usd > 0
                   GROUP BY service
                   ORDER BY cost_usd DESC""") as cur:
                cost_rows = [dict(r) for r in await cur.fetchall()]
        cost_summary = {r["service"]: r for r in cost_rows}
        cost_total = {
            "runs": sum(r["runs"] for r in cost_rows),
            "tokens_in": sum(r["tokens_in"] for r in cost_rows),
            "tokens_out": sum(r["tokens_out"] for r in cost_rows),
            "cost_usd": round(sum(r["cost_usd"] for r in cost_rows), 4),
        }
        # 各 service 的"下次触发时间"（基于 launchd schedule）
        next_schedule = {
            "collect": _next_hour_tick([2, 8, 14, 20], minute=0),
            "alert":   _next_hour_tick([2, 8, 14, 20], minute=30),
            "analyze": "手动触发（在主题详情页）",
            "advisor": "手动触发（在主题详情页）",
        }

        # watch mode：用户刚点了"立即跑"，URL 带 watch=<svc>&watch_started=<ts>
        watch_svc = request.query_params.get("watch")
        watch_started = request.query_params.get("watch_started")
        watch_state = None  # 'running' / 'done' / None
        watch_result = None
        if watch_svc:
            # 是否还有该 service 的 running run？
            running_for_svc = [r for r in running_now if r["service"] == watch_svc]
            if running_for_svc:
                watch_state = "running"
            else:
                # 找 watch_started 之后该 service 最新一条 finished run
                try:
                    started_ts = int(watch_started or 0)
                except ValueError:
                    started_ts = 0
                # 在 runs 里找该 service 最近一条 finished_at 不为 null
                for r in runs:
                    if r["service"] != watch_svc:
                        continue
                    if r.get("finished_at"):
                        watch_state = "done"
                        watch_result = r
                        break

        return templates.TemplateResponse(request=request, name="status.html", context={
            "runs": runs,
            "pause": _service_pause_status(),
            "last_success": last_success,
            "running_now": running_now,
            "next_schedule": next_schedule,
            "limit": limit,
            "cost_summary": cost_summary,
            "cost_total": cost_total,
            "watch_svc": watch_svc,
            "watch_state": watch_state,
            "watch_result": watch_result,
        })

    @app.get("/cost", response_class=HTMLResponse)
    async def cost_page(request: Request):
        """LLM 成本月聚合视图。"""
        db = app.state.db
        await db.init_schema()
        async with aiosqlite.connect(db.db_path) as raw_db:
            raw_db.row_factory = aiosqlite.Row
            async with raw_db.execute(
                """SELECT substr(started_at, 1, 7) AS month,
                          service,
                          COUNT(*) AS runs,
                          COALESCE(SUM(llm_tokens_in), 0) AS tokens_in,
                          COALESCE(SUM(llm_tokens_out), 0) AS tokens_out,
                          COALESCE(SUM(llm_cache_read_tokens), 0) AS cache_read,
                          COALESCE(SUM(llm_cost_usd), 0.0) AS cost_usd
                   FROM service_runs
                   WHERE llm_cost_usd > 0
                   GROUP BY month, service
                   ORDER BY month DESC, cost_usd DESC""") as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        # 按月分组（保持月内 service 顺序）
        by_month: dict[str, list[dict]] = {}
        for r in rows:
            by_month.setdefault(r["month"], []).append(r)
        # 月小计
        month_totals = {
            m: {
                "runs": sum(x["runs"] for x in rs),
                "tokens_in": sum(x["tokens_in"] for x in rs),
                "tokens_out": sum(x["tokens_out"] for x in rs),
                "cost_usd": round(sum(x["cost_usd"] for x in rs), 4),
            }
            for m, rs in by_month.items()
        }
        return templates.TemplateResponse(
            request=request, name="cost.html", context={
                "by_month": by_month,
                "month_totals": month_totals,
            })

    # ============================================================
    # D2: 写入 + 运维操作
    # ============================================================

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "2.0.0"}

    # ---------- sources 写入 ----------

    @app.post("/sources/add")
    async def sources_add(
        request: Request,
        kind: str = Form(...),
        identifier: str = Form(...),
        display_name: str = Form(""),
        skip_resolve: bool = Form(False),
    ):
        """加 source · 默认调 collector.resolve() 验证 identifier。"""
        db = app.state.db
        await db.init_schema()
        auto_discover()

        config = app.state.config
        cfg_dict = config.model_dump()
        identifier = identifier.strip()
        display = display_name.strip() or None

        if kind not in KIND_REGISTRY:
            raise HTTPException(400, f"unknown kind: {kind!r}")

        if not skip_resolve:
            collector_cls = KIND_REGISTRY[kind]
            try:
                meta = await collector_cls.resolve(identifier, cfg_dict)
                if not display:
                    display = meta.get("display_name")
            except Exception as e:
                log.warning("resolve 失败 %s/%s: %s", kind, identifier, e)
                raise HTTPException(400, f"resolve 失败: {e}")

        try:
            await db.insert_source(kind, identifier, display)
        except aiosqlite.IntegrityError:
            raise HTTPException(400, f"已存在: {kind}/{identifier}")

        return RedirectResponse("/sources?msg=added", status_code=303)

    @app.post("/sources/{source_id}/toggle")
    async def sources_toggle(source_id: int, request: Request):
        db = app.state.db
        await db.init_schema()
        s = await db.get_source(source_id)
        if not s:
            raise HTTPException(404, "source 不存在")
        if s["enabled"]:
            await db.disable_source(source_id)
            new_enabled = False
        else:
            await db.enable_source(source_id)
            new_enabled = True
        if request.headers.get("HX-Request"):
            badge = ('<span class="badge ok">enabled</span>'
                     if new_enabled
                     else '<span class="badge cold">disabled</span>')
            return HTMLResponse(badge)
        return RedirectResponse("/sources?msg=toggled", status_code=303)

    @app.post("/sources/{source_id}/delete")
    async def sources_delete(source_id: int):
        db = app.state.db
        await db.init_schema()
        await db.delete_source(source_id)
        return RedirectResponse("/sources?msg=deleted", status_code=303)

    # ---------- topics 写入 ----------

    @app.post("/topics/add")
    async def topics_add(
        request: Request,
        name: str = Form(...),
        industry: str = Form("general"),
        monitor_direction: str = Form(""),
        alert_enabled: bool = Form(True),
        weekly_enabled: bool = Form(True),
        backfill_hours: int = Form(0),
        alert_interval_hours: int = Form(0),
    ):
        db = app.state.db
        await db.init_schema()
        name = name.strip()
        industry = industry.strip() or "general"
        if not name:
            raise HTTPException(400, "name 必填")
        try:
            tid = await db.insert_topic(
                name=name, industry=industry,
                monitor_direction=monitor_direction.strip(),
                alert_enabled=alert_enabled,
                weekly_enabled=weekly_enabled,
                backfill_hours=max(0, int(backfill_hours)),
                alert_interval_hours=max(0, int(alert_interval_hours)),
            )
        except aiosqlite.IntegrityError:
            raise HTTPException(400, f"topic 已存在: {name}")

        form_data = await request.form()
        added_sources = 0

        # 路径 A：从已有信源池勾选关联（source_ids）
        for sid_raw in form_data.getlist("source_ids"):
            try:
                sid = int(sid_raw)
            except (TypeError, ValueError):
                continue
            await db.link_topic_source(tid, sid, skip_keyword_filter=False)
            added_sources += 1

        # 路径 B：AI 推荐项（每条候选是 JSON 字符串：kind/identifier/display_name）
        import json
        for cand_json in form_data.getlist("add_candidate"):
            try:
                c = json.loads(cand_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            kind = (c.get("kind") or "").strip().lower()
            identifier = (c.get("identifier") or "").strip()
            display_name = (c.get("display_name") or "").strip() or None
            if not kind or not identifier:
                continue
            # 复用已有 source 或新建
            existing = None
            async with aiosqlite.connect(db.db_path) as raw:
                raw.row_factory = aiosqlite.Row
                async with raw.execute(
                    "SELECT * FROM sources WHERE kind=? AND identifier=? LIMIT 1",
                    (kind, identifier)) as cur:
                    row = await cur.fetchone()
                    if row:
                        existing = dict(row)
            if existing:
                sid = existing["id"]
            else:
                try:
                    sid = await db.insert_source(kind, identifier, display_name)
                except aiosqlite.IntegrityError:
                    continue
            await db.link_topic_source(tid, sid, skip_keyword_filter=False)
            added_sources += 1

        return RedirectResponse(f"/topics/{tid}?msg=added", status_code=303)

    @app.post("/topics/recommend-sources", response_class=HTMLResponse)
    async def topics_recommend_sources(
        request: Request,
        name: str = Form(""),
        industry: str = Form("general"),
        monitor_direction: str = Form(""),
    ):
        """AI 推荐信源（用 web search）。返回 HTML fragment for htmx swap。"""
        if not (name or monitor_direction).strip():
            return HTMLResponse(
                '<div class="inline-banner warn">'
                '<strong>填一下主题名或监控方向</strong>'
                '<p>AI 需要这些信息才能推荐</p>'
                '</div>')
        from sentinel.services.source_discovery import discover_sources
        config = app.state.config
        try:
            candidates = await discover_sources(
                name=name, industry=industry,
                monitor_direction=monitor_direction,
                config=config,
            )
        except Exception as e:
            log.exception("discover_sources 失败")
            return HTMLResponse(
                f'<div class="inline-banner warn">'
                f'<strong>AI 推荐失败</strong>'
                f'<p>{type(e).__name__}: {e}</p>'
                f'</div>')
        return templates.TemplateResponse(
            request=request, name="_source_candidates.html",
            context={"candidates": candidates})

    @app.post("/topics/{topic_id}/update")
    async def topics_update(
        topic_id: int,
        name: str = Form(...),
        industry: str = Form("general"),
        monitor_direction: str = Form(""),
        backfill_hours: int = Form(0),
        alert_interval_hours: int = Form(0),
    ):
        db = app.state.db
        await db.init_schema()
        topic = await db.get_topic(topic_id)
        if not topic:
            raise HTTPException(404, "topic 不存在")
        try:
            await db.update_topic(
                topic_id,
                name=name, industry=industry,
                monitor_direction=monitor_direction,
                backfill_hours=backfill_hours,
                alert_interval_hours=alert_interval_hours,
            )
        except aiosqlite.IntegrityError:
            raise HTTPException(400, f"主题名冲突: {name}")
        return RedirectResponse(f"/topics/{topic_id}?msg=updated",
                                status_code=303)

    @app.post("/sources/{source_id}/update")
    async def sources_update(
        source_id: int,
        identifier: str = Form(...),
        display_name: str = Form(""),
    ):
        db = app.state.db
        await db.init_schema()
        s = await db.get_source(source_id)
        if not s:
            raise HTTPException(404, "source 不存在")
        try:
            await db.update_source(source_id,
                                   identifier=identifier,
                                   display_name=display_name)
        except aiosqlite.IntegrityError:
            raise HTTPException(400, "identifier 冲突")
        return RedirectResponse("/sources?msg=updated", status_code=303)

    @app.post("/topics/{topic_id}/toggle-weekly")
    async def topics_toggle_weekly(topic_id: int, request: Request):
        db = app.state.db
        await db.init_schema()
        await db.toggle_topic_weekly(topic_id)
        if request.headers.get("HX-Request"):
            t = await db.get_topic(topic_id)
            return HTMLResponse(
                _toggle_badge_html(bool(t["weekly_enabled"]), "weekly"))
        return RedirectResponse("/topics?msg=toggled", status_code=303)

    @app.post("/topics/{topic_id}/toggle-alert")
    async def topics_toggle_alert(topic_id: int, request: Request):
        db = app.state.db
        await db.init_schema()
        await db.toggle_topic_alert(topic_id)
        if request.headers.get("HX-Request"):
            t = await db.get_topic(topic_id)
            return HTMLResponse(
                _toggle_badge_html(bool(t["alert_enabled"]), "alert"))
        return RedirectResponse("/topics?msg=toggled", status_code=303)

    @app.post("/topics/{topic_id}/link")
    async def topics_link(
        topic_id: int,
        source_id: int = Form(...),
        skip_keyword_filter: bool = Form(False),
    ):
        db = app.state.db
        await db.init_schema()
        topic = await db.get_topic(topic_id)
        if not topic:
            raise HTTPException(404, "topic 不存在")
        src = await db.get_source(source_id)
        if not src:
            raise HTTPException(404, "source 不存在")
        await db.link_topic_source(topic_id, source_id, skip_keyword_filter)
        return RedirectResponse(f"/topics/{topic_id}?msg=linked", status_code=303)

    @app.post("/topics/{topic_id}/unlink")
    async def topics_unlink(
        topic_id: int, source_id: int = Form(...),
    ):
        db = app.state.db
        await db.init_schema()
        await db.unlink_topic_source(topic_id, source_id)
        return RedirectResponse(f"/topics/{topic_id}?msg=unlinked", status_code=303)

    @app.post("/topics/{topic_id}/keyword")
    async def topics_add_keyword(topic_id: int, keyword: str = Form(...)):
        db = app.state.db
        await db.init_schema()
        keyword = keyword.strip()
        if not keyword:
            raise HTTPException(400, "keyword 不能为空")
        await db.add_topic_keyword(topic_id, keyword)
        return RedirectResponse(f"/topics/{topic_id}?msg=kw_added", status_code=303)

    @app.post("/topics/{topic_id}/recommend-keywords", response_class=HTMLResponse)
    async def topics_recommend_keywords(request: Request, topic_id: int):
        """AI 推荐 topic 关键词（htmx fragment）。"""
        from sentinel.services.keyword_advisor import recommend_keywords
        db = app.state.db
        await db.init_schema()
        config = app.state.config
        try:
            result = await recommend_keywords(topic_id, db, config=config)
        except Exception as e:
            log.exception("recommend_keywords 失败")
            return HTMLResponse(
                f'<div class="inline-banner warn">'
                f'<strong>AI 推荐失败</strong>'
                f'<p>{type(e).__name__}: {e}</p></div>')
        return templates.TemplateResponse(
            request=request, name="_keyword_candidates.html",
            context={"topic_id": topic_id, "result": result})

    @app.post("/topics/{topic_id}/keywords/bulk-add")
    async def topics_keywords_bulk_add(request: Request, topic_id: int):
        """一次添加多个关键词（form 多个 name=kw 字段）。"""
        db = app.state.db
        await db.init_schema()
        form_data = await request.form()
        added = 0
        for kw in form_data.getlist("kw"):
            kw = kw.strip()
            if kw:
                await db.add_topic_keyword(topic_id, kw)
                added += 1
        if added:
            return RedirectResponse(
                f"/topics/{topic_id}?msg=kw_added", status_code=303)
        return RedirectResponse(
            f"/topics/{topic_id}?msg=executed_empty", status_code=303)

    @app.post("/topics/{topic_id}/keyword/remove")
    async def topics_remove_keyword(
        topic_id: int, keyword: str = Form(...),
    ):
        db = app.state.db
        await db.init_schema()
        await db.remove_topic_keyword(topic_id, keyword)
        return RedirectResponse(f"/topics/{topic_id}?msg=kw_removed", status_code=303)

    @app.post("/topics/{topic_id}/delete")
    async def topics_delete(topic_id: int):
        db = app.state.db
        await db.init_schema()
        await db.delete_topic(topic_id)
        return RedirectResponse("/topics?msg=deleted", status_code=303)

    @app.get("/topics/{topic_id}", response_class=HTMLResponse)
    async def topic_detail(request: Request, topic_id: int,
                          action: str | None = None,
                          just_executed: int | None = None):
        """单 topic 详情页 · 含信源关联 + 关键词管理 + 覆盖修复面板。"""
        import json as _json
        db = app.state.db
        await db.init_schema()
        topic = await db.get_topic(topic_id)
        if not topic:
            raise HTTPException(404, "topic 不存在")
        topic["_sources"] = await db.list_topic_sources(topic_id)
        topic["_keywords"] = await db.list_topic_keywords(topic_id)
        all_sources = await db.list_all_sources()
        linked_ids = {s["id"] for s in topic["_sources"]}
        unlinked = [s for s in all_sources if s["id"] not in linked_ids]

        # 最近一次 audit（用于 fix-coverage 面板）
        latest_audit = await db.latest_coverage_audit(topic_id)
        if latest_audit:
            try:
                latest_audit["_metrics"] = _json.loads(latest_audit.get("metrics_json") or "{}")
                latest_audit["_rsshub"] = _json.loads(latest_audit.get("rsshub_suggestions_json") or "[]")
                latest_audit["_webaccess"] = _json.loads(latest_audit.get("webaccess_suggestions_json") or "[]")
            except (TypeError, ValueError):
                pass

        # 如果用户刚执行 audit (URL ?just_executed=N), 拉那次 executed 的 audit
        # 找出该 audit 后新加的 source 用于显示
        just_executed_summary = None
        if just_executed:
            async with aiosqlite.connect(db.db_path) as raw:
                raw.row_factory = aiosqlite.Row
                async with raw.execute(
                    "SELECT * FROM topic_coverage_audit WHERE id=?",
                    (just_executed,)) as cur:
                    je = await cur.fetchone()
                if je and je["topic_id"] == topic_id:
                    je_dict = dict(je)
                    # 找出该 audit executed 后新建的 source（按 created_at >= executed_at - 5s）
                    if je_dict.get("executed_at"):
                        async with raw.execute(
                            """SELECT s.* FROM sources s
                               JOIN topic_sources ts ON ts.source_id=s.id
                               WHERE ts.topic_id=?
                                 AND s.created_at >= datetime(?, '-5 seconds')
                                 AND s.created_at <= datetime(?, '+5 seconds')
                               ORDER BY s.id""",
                            (topic_id, je_dict["executed_at"],
                             je_dict["executed_at"])) as cur:
                            new_sources = [dict(r) for r in await cur.fetchall()]
                        just_executed_summary = {
                            "audit": je_dict,
                            "new_sources": new_sources,
                            "notes": je_dict.get("notes", ""),
                        }

        return templates.TemplateResponse(
            request=request, name="topic_detail.html", context={
                "topic": topic, "unlinked": unlinked,
                "latest_audit": latest_audit,
                "auto_expand_audit": action == "fix-coverage",
                "just_executed_summary": just_executed_summary,
            })

    # ---------- service 运维操作 ----------

    @app.post("/services/{svc}/pause")
    async def service_pause(svc: str, days: int = Form(0)):
        if svc not in ("collect", "alert", "analyze", "advisor"):
            raise HTTPException(400, f"unknown service: {svc!r}")
        pause_file = PROJECT_ROOT / f"{PAUSE_FILE_PREFIX}{svc}"
        lines = [f"# Sentinel v2 {svc} service · 暂停标记"]
        if days > 0:
            expire = (datetime.now(timezone.utc) +
                      timedelta(days=days)).isoformat()
            lines.append(f"expire_at={expire}")
        pause_file.write_text("\n".join(lines) + "\n")
        return RedirectResponse("/status?msg=paused", status_code=303)

    @app.post("/services/{svc}/resume")
    async def service_resume(svc: str):
        pause_file = PROJECT_ROOT / f"{PAUSE_FILE_PREFIX}{svc}"
        if pause_file.exists():
            pause_file.unlink()
        return RedirectResponse("/status?msg=resumed", status_code=303)

    @app.post("/services/{svc}/run")
    async def service_run(
        svc: str, background_tasks: BackgroundTasks,
        topic: str = Form(""),
        period_hours: int = Form(168),
        mode: str = Form("auto"),
    ):
        """手动触发 service run（异步后台跑）。

        BackgroundTasks 直接接受 async function（同一 event loop）。
        触发后 redirect 带 watch=1：status 页会自动 polling 直到跑完。
        """
        config = app.state.config
        db = app.state.db
        await db.init_schema()

        if svc == "collect":
            from sentinel.services.collect import run_collect_service
            background_tasks.add_task(run_collect_service, config, db, True)
        elif svc == "alert":
            from sentinel.services.alert import run_alert_service
            background_tasks.add_task(run_alert_service, config, db, True)
        elif svc == "analyze":
            if not topic:
                raise HTTPException(400, "analyze 需要 topic 参数")
            from sentinel.services.analyze import run_analyze_service
            background_tasks.add_task(
                run_analyze_service, config, db,
                topic_name=topic, period_hours=period_hours, mode=mode)
        elif svc == "advisor":
            if not topic:
                raise HTTPException(400, "advisor 需要 topic 参数")
            from sentinel.services.advisor import run_advisor_service
            background_tasks.add_task(
                run_advisor_service, config, db, topic_name=topic)
        else:
            raise HTTPException(400, f"unknown service: {svc!r}")

        # watch=<svc>&watch_started=<epoch ms> 让 /status 知道刚触发的是谁
        import time
        return RedirectResponse(
            f"/status?msg=triggered&watch={svc}&watch_started={int(time.time())}",
            status_code=303)

    # ---------- alert 标记 ----------

    @app.post("/alerts/{alert_id}/label")
    async def alert_label(alert_id: int, label: str = Form(...)):
        if label not in ("true_positive", "false_positive", "clear"):
            raise HTTPException(400, "invalid label")
        db = app.state.db
        await db.init_schema()
        await db.label_alert(
            alert_id, None if label == "clear" else label)
        return RedirectResponse("/alerts?msg=labeled", status_code=303)

    # ---------- coverage audit ----------

    @app.post("/coverage-audit/run")
    async def coverage_audit_run(
        request: Request,
        background_tasks: BackgroundTasks,
        skip_llm: bool = Form(False),
    ):
        """手动触发全体 audit · 后台跑，立即 redirect。"""
        from sentinel.services.coverage_audit import run_coverage_audit
        config = app.state.config
        db = app.state.db
        await db.init_schema()
        background_tasks.add_task(run_coverage_audit, config, db,
                                  skip_llm=skip_llm)
        return RedirectResponse("/?msg=triggered", status_code=303)

    @app.post("/coverage-audit/{audit_id}/dismiss")
    async def coverage_audit_dismiss(
        audit_id: int,
        days: str = Form("30"),  # '7' / '30' / 'forever'
    ):
        db = app.state.db
        await db.init_schema()
        if days == "forever":
            dismissed_until = None
        else:
            try:
                d = int(days)
            except ValueError:
                d = 30
            dismissed_until = (datetime.now(timezone.utc) +
                               timedelta(days=d)).isoformat()
        await db.dismiss_coverage_audit(audit_id, dismissed_until)
        return RedirectResponse("/?msg=triggered", status_code=303)

    @app.post("/topics/{topic_id}/run-coverage-fix")
    async def topics_run_coverage_fix(
        request: Request,
        topic_id: int,
        audit_id: int = Form(...),
    ):
        """执行 audit 建议:
        - 选中的 rsshub_urls (form list) → 自动 insert source + link 到 topic
        - 选中的 webaccess_keywords → 生成 prompt 让用户去 Claude 跑（fallback）
          复制 prompt 到 toast 提示用
        """
        import json as _json
        db = app.state.db
        await db.init_schema()

        form_data = await request.form()
        rsshub_urls = form_data.getlist("rsshub_url")
        webaccess_picks = form_data.getlist("webaccess_pick")  # 'platform|keyword'

        added_sources = 0
        for url_str in rsshub_urls:
            url = url_str.strip()
            if not url or not url.startswith(("http://", "https://")):
                continue
            # 查或建 source
            existing_sid: int | None = None
            async with aiosqlite.connect(db.db_path) as raw:
                raw.row_factory = aiosqlite.Row
                async with raw.execute(
                    "SELECT id FROM sources WHERE kind='rss' AND identifier=?",
                    (url,)) as cur:
                    row = await cur.fetchone()
                    if row:
                        existing_sid = row["id"]
            if existing_sid is None:
                try:
                    existing_sid = await db.insert_source("rss", url, None)
                except aiosqlite.IntegrityError:
                    continue
            await db.link_topic_source(topic_id, existing_sid,
                                       skip_keyword_filter=False)
            added_sources += 1

        # 标 audit 为 executed
        notes_parts = []
        if added_sources:
            notes_parts.append(f"加 {added_sources} 个 rsshub source")
        if webaccess_picks:
            notes_parts.append(f"web-access picks: {len(webaccess_picks)}")
        await db.execute_coverage_audit(audit_id, "; ".join(notes_parts) or None)

        msg = "executed" if added_sources else "executed_empty"
        # 把刚执行的 audit_id 带过去，详情页可以高亮显示这次加了什么
        return RedirectResponse(
            f"/topics/{topic_id}?msg={msg}&just_executed={audit_id}",
            status_code=303)
