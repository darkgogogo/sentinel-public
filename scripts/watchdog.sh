#!/bin/bash
# Sentinel v2 watchdog · 巡检 collect / alert / backup / weekly + 自身 heartbeat
#
# 告警条件（任一命中即 TG push）：
#   - collect 距上次成功 > COLLECT_THRESHOLD_H (默认 36h)
#   - alert   距上次成功 > ALERT_THRESHOLD_H   (默认 30h)
#   - backup  目录最新文件 mtime > BACKUP_THRESHOLD_H (默认 48h)
#   - 上周日 22:00 后无新周报产出（周一 09:00 检查）
#
# 副机的 service_runs 全是 host_mismatch skip → 视为预期，不告警。
# 自身正常时（无告警）每周一 + 每月 1 号发一条 heartbeat 让用户知道 watchdog 活着。

set -e

SENTINEL_HOME="${SENTINEL_HOME:-$HOME/sentinel-v2}"
cd "$SENTINEL_HOME"

COLLECT_THRESHOLD_H="${COLLECT_THRESHOLD_H:-36}"
ALERT_THRESHOLD_H="${ALERT_THRESHOLD_H:-30}"
BACKUP_THRESHOLD_H="${BACKUP_THRESHOLD_H:-48}"

"$SENTINEL_HOME/.venv/bin/python" - <<PYEOF
import asyncio
import os
import sys
import glob
from pathlib import Path
from datetime import datetime, timedelta, timezone

from sentinel.config import DB_PATH, ENV_PATH, CONFIG_PATH, KB_ROOT, load_config
from sentinel.db import Database

import aiosqlite
import httpx


COLLECT_TH = float(os.environ.get("COLLECT_THRESHOLD_H", "36"))
ALERT_TH = float(os.environ.get("ALERT_THRESHOLD_H", "30"))
BACKUP_TH = float(os.environ.get("BACKUP_THRESHOLD_H", "48"))

BACKUP_DIR = KB_ROOT / "_archive" / "backups"
WEEKLY_DIR = KB_ROOT / "01-报告" / "周报"


def now_utc():
    return datetime.now(timezone.utc)


async def hours_since_last_success(db_path: str, service: str) -> float | None:
    """距上次 success run 多少小时；从未跑过返回 None。

    跳过 host_mismatch skipped run（副机视为预期）。
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT started_at FROM service_runs "
            "WHERE service=? AND status='success' "
            "ORDER BY started_at DESC LIMIT 1",
            (service,)) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    started = datetime.fromisoformat(row["started_at"])
    return (now_utc() - started).total_seconds() / 3600


def hours_since_last_backup() -> float | None:
    """最新 messages-*.db.gz mtime 距今多少小时；目录为空返回 None。"""
    if not BACKUP_DIR.exists():
        return None
    files = sorted(BACKUP_DIR.glob("messages-*.db.gz"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    latest = files[0].stat().st_mtime
    return (datetime.now().timestamp() - latest) / 3600


def weekly_report_missing() -> bool:
    """周一 09:00 检查上周日 22:00 后有没有产新周报。

    周一以外不检查（避免周中误告）。
    """
    if datetime.now().isoweekday() != 1:  # 1=周一
        return False
    if not WEEKLY_DIR.exists():
        return False
    last_sunday_22 = (datetime.now()
                      .replace(hour=22, minute=0, second=0, microsecond=0)
                      - timedelta(days=1))
    for p in WEEKLY_DIR.glob("*.md"):
        if datetime.fromtimestamp(p.stat().st_mtime) >= last_sunday_22:
            return False
    return True


def tg_push(msg: str, config) -> None:
    bot = config.secrets.get("TELEGRAM_BOT_TOKEN", "")
    chat = config.secrets.get("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        print(f"[watchdog] ⚠ TG creds 未配置，警告无法 push: {msg}")
        return
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={"chat_id": chat, "text": msg}, timeout=15)
        print(f"[watchdog] TG push status={resp.status_code}")
    except Exception as e:
        print(f"[watchdog] TG push 失败: {e}")


async def main() -> int:
    db = Database(str(DB_PATH))
    await db.init_schema()
    config = load_config(CONFIG_PATH, ENV_PATH)

    # 副机自跳：当前机器不是 deploy host 时不告警（host_mismatch skip 是预期，不污染 push）
    if config.host_check_enabled:
        from sentinel.deploy import check_host_match, _local_hostname
        is_main, deploy_host = check_host_match()
        if not is_main:
            print(f"[watchdog] ○ host_mismatch: deploy={deploy_host} "
                  f"local={_local_hostname()} → 副机自跳，不告警")
            return 0

    alerts: list[str] = []

    # 1. collect
    gap = await hours_since_last_success(str(DB_PATH), "collect")
    if gap is None:
        alerts.append("collect 从未成功跑过")
    elif gap > COLLECT_TH:
        alerts.append(f"collect 距上次成功 {gap:.1f}h (阈值 {COLLECT_TH:.0f}h)")
    else:
        print(f"[watchdog] ✓ collect ok ({gap:.1f}h)")

    # 2. alert
    gap = await hours_since_last_success(str(DB_PATH), "alert")
    if gap is None:
        alerts.append("alert 从未成功跑过")
    elif gap > ALERT_TH:
        alerts.append(f"alert 距上次成功 {gap:.1f}h (阈值 {ALERT_TH:.0f}h)")
    else:
        print(f"[watchdog] ✓ alert ok ({gap:.1f}h)")

    # 3. backup（新部署 grace：从未跑过不告警，跑过但 stale 才告警）
    bgap = hours_since_last_backup()
    if bgap is None:
        print(f"[watchdog] ○ backup 暂无文件 (新部署预期，等首次 23:30 跑)")
    elif bgap > BACKUP_TH:
        alerts.append(f"backup 最新 {bgap:.1f}h ago (阈值 {BACKUP_TH:.0f}h)")
    else:
        print(f"[watchdog] ✓ backup ok ({bgap:.1f}h ago)")

    # 4. weekly report missing
    if weekly_report_missing():
        alerts.append("周一巡检：上周日 22:00 后无新周报产出")
    else:
        print("[watchdog] ✓ weekly report check ok")

    # 5. coverage audit (周一 09:00 才跑，其他日子跳过避免烧 LLM)
    today = datetime.now()
    coverage_lines: list[str] = []
    if today.isoweekday() == 1:
        from sentinel.services.coverage_audit import run_coverage_audit
        try:
            audit_results = await run_coverage_audit(config, db)
            high_topics = [r for r in audit_results if r.get("severity") == "high"]
            medium_topics = [r for r in audit_results if r.get("severity") == "medium"]
            print(f"[watchdog] ✓ coverage audit: "
                  f"{len(high_topics)} high · {len(medium_topics)} medium")
            if high_topics:
                lines = [f"⚠ 覆盖审计 · {len(high_topics)} 个主题信号严重不足:"]
                for r in high_topics[:8]:
                    m = r.get("metrics", {})
                    lines.append(
                        f"  • {r['topic_name']}  "
                        f"(7d {m.get('msgs_7d', 0)} msgs, "
                        f"{m.get('platform_count', 0)} platform)")
                lines.append("")
                lines.append("→ http://127.0.0.1:8080/  查看修复建议")
                coverage_lines = lines
        except Exception as e:
            print(f"[watchdog] ⚠ coverage audit 失败: {e}")
    else:
        print(f"[watchdog] ○ coverage audit 跳过 (非周一)")

    # ===== 处理结果 =====
    # 健康问题 push（最高优先级）
    if alerts:
        msg = "⚠ Sentinel watchdog 发现问题:\n- " + "\n- ".join(alerts)
        print(f"[watchdog] {msg}")
        tg_push(msg, config)
        # 如果还有 coverage 信息，跟健康问题合并推一条
        if coverage_lines:
            tg_push("\n".join(coverage_lines), config)
        return 2

    # 单独 coverage push（无健康问题但有覆盖建议）
    if coverage_lines:
        msg = "\n".join(coverage_lines)
        print(f"[watchdog] {msg}")
        tg_push(msg, config)
        return 0

    # 全 ok → 周一 / 月初发一条 heartbeat 让用户知道 watchdog 活着
    today = datetime.now()
    is_monday = today.isoweekday() == 1
    is_month_start = today.day == 1
    if is_monday or is_month_start:
        tag = []
        if is_monday: tag.append("周一")
        if is_month_start: tag.append("月初")
        msg = (f"✓ Sentinel watchdog heartbeat ({' / '.join(tag)})\n"
               f"  collect / alert / backup / weekly 全部正常")
        tg_push(msg, config)
        print(f"[watchdog] heartbeat sent: {tag}")
    return 0


sys.exit(asyncio.run(main()))
PYEOF
