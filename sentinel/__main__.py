"""Sentinel v2 CLI 入口。

Phase A 实现：collect run/pause/resume + status。
Phase B/C/D 实施时扩展 alert / analyze / advisor / topic / source 子命令。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sentinel.config import (
    CONFIG_PATH, ENV_PATH, DB_PATH, PROJECT_ROOT, load_config,
)
from sentinel.db import Database
from sentinel.runtime.shell import PAUSE_FILE_PREFIX


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main() -> int:
    parser = argparse.ArgumentParser(prog="sentinel")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # collect 子命令组
    p_collect = sub.add_parser("collect", help="采集 service")
    sub_c = p_collect.add_subparsers(dest="action", required=True)

    p_run = sub_c.add_parser("run", help="跑一次采集")
    p_run.add_argument("--force", action="store_true",
                       help="跳过阈值守门，强制跑（仍尊重 .pause-collect）")

    p_pause = sub_c.add_parser("pause", help="暂停采集 service")
    p_pause.add_argument("--days", type=int, default=None,
                         help="暂停 N 天后自动恢复；不填 = 永久")

    sub_c.add_parser("resume", help="恢复采集 service")

    # alert 子命令组
    p_alert = sub.add_parser("alert", help="告警 service")
    sub_a = p_alert.add_subparsers(dest="action", required=True)

    p_arun = sub_a.add_parser("run", help="跑一次告警判定")
    p_arun.add_argument("--force", action="store_true",
                        help="跳过阈值守门，强制跑")
    p_arun.add_argument("--dry-run-push", action="store_true",
                        help="跑完流程但 push 不真发（log 输出）")

    p_apause = sub_a.add_parser("pause", help="暂停告警 service")
    p_apause.add_argument("--days", type=int, default=None,
                          help="暂停 N 天后自动恢复；不填 = 永久")

    sub_a.add_parser("resume", help="恢复告警 service")

    # analyze 子命令组（人工触发深度报告 ★）
    p_analyze = sub.add_parser("analyze", help="分析 service · 出深度报告 ★")
    sub_an = p_analyze.add_subparsers(dest="action", required=True)

    p_anrun = sub_an.add_parser("report", help="跑一次深度报告")
    p_anrun.add_argument("--topic", required=True, help="topic 名（必填）")
    p_anrun.add_argument("--period", default="168h",
                         help="时间窗，如 24h / 168h(7天) / 720h(30天)")
    p_anrun.add_argument("--mode", default="auto",
                         choices=["auto", "single", "timeseries"],
                         help="single = 一次性出报告；timeseries = 聚类+议题深度")

    # analyze weekly：所有 alert_enabled topic 跑 7d 周报（launchd 调用）
    p_weekly = sub_an.add_parser("weekly",
                                  help="周报模式 · 遍历所有 alert_enabled topic 跑 7d 深度报告")

    # advisor 子命令组
    p_advisor = sub.add_parser("advisor", help="advisor service · 信源运营建议")
    sub_ad = p_advisor.add_subparsers(dest="action", required=True)
    p_adscan = sub_ad.add_parser("scan", help="跑一次 advisor scan，写信源.md")
    p_adscan.add_argument("--topic", required=True, help="topic 名（必填）")
    p_adscan.add_argument("--window-days", type=int, default=30,
                          help="反馈数据窗口（默认 30 天）")

    # migrate-v1 子命令 · 从 v1 sqlite 导入 source/topic 配置
    p_migrate = sub.add_parser(
        "migrate-v1",
        help="v1 → v2 信源 / 主题配置迁移（默认 dry-run）")
    p_migrate.add_argument("--apply", action="store_true",
                           help="实际写入（默认 dry-run 只看会做什么）")
    p_migrate.add_argument("--v1-db", default=None,
                           help="v1 sqlite 路径（从 v1 sentinel 项目导入数据）")

    # deploy 子命令组 · launchd 部署
    p_deploy = sub.add_parser("deploy", help="launchd 部署 (Phase E)")
    sub_dp = p_deploy.add_subparsers(dest="action", required=True)
    sub_dp.add_parser("install", help="渲染 plist + launchctl load + 写 .host")
    sub_dp.add_parser("uninstall", help="unload + 移除 plist")
    sub_dp.add_parser("status", help="看部署状态")

    # web 子命令 · 启 dashboard
    p_web = sub.add_parser("web", help="启 Web dashboard (D1 只读)")
    p_web.add_argument("--host", default="127.0.0.1",
                       help="监听地址（默认仅本机）")
    p_web.add_argument("--port", type=int, default=8080)
    p_web.add_argument("--reload", action="store_true",
                       help="开发模式：代码变化自动重启")

    # status
    p_status = sub.add_parser("status", help="查询 service 运行状态")
    p_status.add_argument("--service", default=None,
                          help="只看某个 service（collect/alert/analyze/advisor）")

    args = parser.parse_args()

    if args.cmd == "collect":
        return asyncio.run(_handle_collect(args))
    if args.cmd == "alert":
        return asyncio.run(_handle_alert(args))
    if args.cmd == "analyze":
        return asyncio.run(_handle_analyze(args))
    if args.cmd == "advisor":
        return asyncio.run(_handle_advisor(args))
    if args.cmd == "status":
        return asyncio.run(_handle_status(args))
    if args.cmd == "deploy":
        return _handle_deploy(args)
    if args.cmd == "migrate-v1":
        from sentinel.migrate import run_migrate
        return asyncio.run(run_migrate(args.v1_db, apply=args.apply))
    if args.cmd == "web":
        return _handle_web(args)

    return 1


def _handle_deploy(args) -> int:
    from sentinel import deploy
    if args.action == "install":
        result = deploy.install()
        print(f"\n=== deploy install 结果 ===")
        print(f"  host: {result['host']}")
        print(f"  installed: {result['installed']}")
        if result["failed"]:
            print(f"  ⚠ failed: {result['failed']}")
        return 0
    if args.action == "uninstall":
        result = deploy.uninstall()
        print(f"\n=== deploy uninstall 结果 ===")
        print(f"  removed: {result['removed']}")
        return 0
    if args.action == "status":
        result = deploy.status()
        print(f"\n=== deploy status ===")
        print(f"  .host file exists: {result['host_file_exists']}")
        print(f"  deploy host: {result['deploy_host']}")
        print(f"  local hostname: {result['local_hostname']}")
        print(f"  is main host: {result['is_main_host']}")
        print(f"  services:")
        for s in result["services"]:
            badge = "✓" if s["loaded"] else ("○" if s["plist_exists"] else "—")
            print(f"    {badge} {s['label']}"
                  f" {'[loaded]' if s['loaded'] else '[plist 已写]' if s['plist_exists'] else '[未部署]'}")
        return 0
    return 1


async def _handle_advisor(args) -> int:
    if args.action == "scan":
        from sentinel.services.advisor import run_advisor_service
        config = load_config(CONFIG_PATH, ENV_PATH)
        db = Database(str(DB_PATH))
        await db.init_schema()
        result = await run_advisor_service(
            config, db, topic_name=args.topic,
            window_days=args.window_days)
        print(f"\n=== advisor scan 结果 ===")
        for k, v in result.items():
            print(f"  {k}: {v}")
        return 0 if result.get("status") == "success" else 1
    return 1


def _handle_web(args) -> int:
    import uvicorn
    print(f"\n=== Sentinel v2 dashboard ===")
    print(f"  http://{args.host}:{args.port}")
    print(f"  Ctrl+C 退出\n")
    if args.reload:
        # reload 模式需要 import string
        uvicorn.run("sentinel.web.app:build_app", factory=True,
                    host=args.host, port=args.port, reload=True)
    else:
        from sentinel.web import build_app
        uvicorn.run(build_app(), host=args.host, port=args.port,
                    log_level="info")
    return 0


def _parse_period(s: str) -> int:
    """'24h' / '7d' / '168' → int hours."""
    s = s.strip().lower()
    if s.endswith("h"):
        return int(s[:-1])
    if s.endswith("d"):
        return int(s[:-1]) * 24
    return int(s)


async def _handle_analyze(args) -> int:
    if args.action == "report":
        from sentinel.services.analyze import run_analyze_service
        config = load_config(CONFIG_PATH, ENV_PATH)
        db = Database(str(DB_PATH))
        await db.init_schema()
        result = await run_analyze_service(
            config, db,
            topic_name=args.topic,
            period_hours=_parse_period(args.period),
            mode=args.mode,
        )
        print(f"\n=== analyze report 结果 ===")
        for k, v in result.items():
            print(f"  {k}: {v}")
        return 0 if result.get("status") == "success" else 1

    if args.action == "weekly":
        from sentinel.services.analyze import run_analyze_service
        config = load_config(CONFIG_PATH, ENV_PATH)
        db = Database(str(DB_PATH))
        await db.init_schema()
        topics = await db.list_weekly_topics()
        if not topics:
            print("⚠ 无 weekly_enabled topic，跳过 weekly")
            return 0
        print(f"=== analyze weekly · {len(topics)} 个 topic ===")
        succeeded = 0
        for t in topics:
            print(f"\n→ topic [{t['name']}] industry={t['industry']}")
            result = await run_analyze_service(
                config, db,
                topic_name=t["name"],
                period_hours=168, mode="auto",
            )
            if result.get("status") == "success":
                succeeded += 1
                print(f"  ✓ {result.get('headline','?')}")
                print(f"    {result.get('file_path','?')}")
            else:
                print(f"  ✗ {result.get('error','?')}")
        print(f"\n=== 完成 {succeeded}/{len(topics)} ===")
        return 0

    return 1


async def _handle_alert(args) -> int:
    if args.action == "run":
        from sentinel.services.alert import run_alert_service
        config = load_config(CONFIG_PATH, ENV_PATH)
        db = Database(str(DB_PATH))
        await db.init_schema()
        result = await run_alert_service(
            config, db, force=args.force, dry_run_push=args.dry_run_push)
        print(f"\n=== alert 结果 ===")
        for k, v in result.items():
            print(f"  {k}: {v}")
        return 0

    pause_file = PROJECT_ROOT / f"{PAUSE_FILE_PREFIX}alert"
    if args.action == "pause":
        lines = ["# Sentinel v2 alert service · 暂停标记"]
        if args.days is not None:
            expire = (datetime.now(timezone.utc) +
                      timedelta(days=args.days)).isoformat()
            lines.append(f"expire_at={expire}")
            msg = f"暂停 {args.days} 天（至 {expire}）"
        else:
            msg = "永久暂停（删此文件可恢复）"
        pause_file.write_text("\n".join(lines) + "\n")
        print(f"✓ alert service 已暂停 — {msg}")
        return 0

    if args.action == "resume":
        if pause_file.exists():
            pause_file.unlink()
            print(f"✓ alert service 已恢复")
        else:
            print("alert service 未在暂停中")
        return 0

    return 1


async def _handle_collect(args) -> int:
    if args.action == "run":
        from sentinel.services.collect import run_collect_service
        config = load_config(CONFIG_PATH, ENV_PATH)
        db = Database(str(DB_PATH))
        await db.init_schema()
        result = await run_collect_service(config, db, force=args.force)
        print(f"\n=== collect 结果 ===")
        for k, v in result.items():
            print(f"  {k}: {v}")
        return 0

    if args.action == "pause":
        pause_file = PROJECT_ROOT / f"{PAUSE_FILE_PREFIX}collect"
        lines = ["# Sentinel v2 collect service · 暂停标记"]
        if args.days is not None:
            expire = (datetime.now(timezone.utc) +
                      timedelta(days=args.days)).isoformat()
            lines.append(f"expire_at={expire}")
            msg = f"暂停 {args.days} 天（至 {expire}）"
        else:
            msg = "永久暂停（删此文件可恢复）"
        pause_file.write_text("\n".join(lines) + "\n")
        print(f"✓ collect service 已暂停 — {msg}")
        print(f"  文件: {pause_file}")
        return 0

    if args.action == "resume":
        pause_file = PROJECT_ROOT / f"{PAUSE_FILE_PREFIX}collect"
        if pause_file.exists():
            pause_file.unlink()
            print(f"✓ collect service 已恢复 — 删除 {pause_file}")
        else:
            print("collect service 未在暂停中，无需操作")
        return 0

    return 1


async def _handle_status(args) -> int:
    config = load_config(CONFIG_PATH, ENV_PATH)
    db = Database(str(DB_PATH))
    await db.init_schema()

    services = [args.service] if args.service else [
        "collect", "alert", "analyze", "advisor"]
    print("=== service 运行状态 ===")
    for svc in services:
        last = await db.last_successful_run(svc)
        pause_file = PROJECT_ROOT / f"{PAUSE_FILE_PREFIX}{svc}"
        paused = " [PAUSED]" if pause_file.exists() else ""
        if last:
            print(f"  {svc}{paused}: 上次成功 {last['started_at']} "
                  f"(messages={last.get('messages_collected', 0)})")
        else:
            print(f"  {svc}{paused}: 无成功记录")
    return 0


if __name__ == "__main__":
    sys.exit(main())
