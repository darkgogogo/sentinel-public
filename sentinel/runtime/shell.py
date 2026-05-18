"""ServiceShell — 运维外壳，包装 service 业务核心。

aihot 原则 #3 落地点：把横切的运维关注点（pause / 阈值 / retry / caffeinate / run 记录 /
致命错 push / retention）全收到这里，**业务核心代码只关心业务**。

用法（业务 service 写法）：

    from sentinel.runtime.shell import ServiceShell

    async def run_collect_service(config):
        async with ServiceShell("collect", config) as shell:
            if not shell.should_run:
                return  # paused / 阈值不到 / 等
            # ... 业务核心
            shell.record(messages_collected=42)
"""
from __future__ import annotations

import os
import signal
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sentinel.config import PROJECT_ROOT, Config
from sentinel.db import Database


PAUSE_FILE_PREFIX = ".pause-"          # .pause-collect / .pause-alert / ...


@dataclass
class ShellState:
    """ServiceShell 运行时状态，业务核心可读可写。"""
    service: str
    should_run: bool = True             # False = pause / 阈值不到，业务核心 early-return
    skip_reason: str | None = None
    lookback_hours: float | None = None
    run_id: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stats: dict[str, Any] = field(default_factory=dict)


def check_pause(service: str, project_root: Path = PROJECT_ROOT) -> tuple[bool, str | None]:
    """检查 `.pause-<service>` 文件。

    - 不存在 → (False, None) 正常跑
    - 存在且 expire_at 未到 → (True, expire_at)
    - 存在但 expire_at 已过 → 自动 rm，返回 (False, None)
    - 存在无 expire_at（或非法） → (True, None) 永久暂停
    """
    p = project_root / f"{PAUSE_FILE_PREFIX}{service}"
    if not p.exists():
        return False, None

    content = p.read_text().strip()
    expire_at = None
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("expire_at="):
            expire_at = line[len("expire_at="):].strip()
            break

    if not expire_at:
        return True, None

    try:
        expire_dt = datetime.fromisoformat(expire_at)
        if expire_dt.tzinfo is None:
            expire_dt = expire_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True, None

    if datetime.now(timezone.utc) >= expire_dt:
        try:
            p.unlink()
        except OSError:
            pass
        return False, None

    return True, expire_at


def fork_caffeinate() -> subprocess.Popen | None:
    """fork `caffeinate -di -w <pid>` 防 Mac 睡眠期间冻结 service 进程。

    -d   不让显示睡眠
    -i   不让系统空闲睡眠
    -w   监听本进程 PID，本进程退出后 caffeinate 自动跟着退
    """
    try:
        proc = subprocess.Popen(
            ["caffeinate", "-di", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc
    except (FileNotFoundError, OSError):
        return None


class ServiceShell:
    """运维外壳 · async context manager。

    包装一次 service 跑：进入时做 pause/阈值/caffeinate/run 记录前置；
    退出时根据是否异常更新 service_runs 记录。
    """

    def __init__(self, service: str, config: Config, db: Database,
                 force: bool = False):
        self.service = service
        self.config = config
        self.db = db
        self.force = force          # 跳过 12h 阈值，仍尊重 pause
        self.state = ShellState(service=service)
        self._caffeinate_proc: subprocess.Popen | None = None
        self._llm_acc: list[dict] | None = None   # LLM usage 累加器

    @property
    def should_run(self) -> bool:
        return self.state.should_run

    def record(self, **stats: Any) -> None:
        """业务核心调用：记录统计（messages_collected / alerts_triggered 等）。"""
        self.state.stats.update(stats)

    async def __aenter__(self) -> "ServiceShell":
        # 0. host check（副机退出但写一行 skipped 便于排错 / watchdog 识别）
        from sentinel.deploy import check_host_match, _local_hostname
        if self.config.host_check_enabled:
            is_main, deploy_host = check_host_match()
            if not is_main:
                local = _local_hostname()
                self.state.should_run = False
                self.state.skip_reason = (
                    f"host_mismatch:deploy={deploy_host},local={local}")
                self.state.run_id = await self.db.insert_run(
                    service=self.service, status="skipped",
                    error=self.state.skip_reason)
                return self

        # 1. caffeinate
        self._caffeinate_proc = fork_caffeinate()

        # 2. pause 检查
        paused, expire_at = check_pause(self.service)
        if paused:
            self.state.should_run = False
            self.state.skip_reason = (
                f"paused (expire_at={expire_at})" if expire_at else "paused (indefinite)"
            )
            self.state.run_id = await self.db.insert_run(
                service=self.service, status="skipped",
                error=self.state.skip_reason)
            return self

        # 3. 阈值守门（collect/alert 用，analyze/advisor 是 pull-on-demand 不查阈值）
        threshold_hours = self._get_threshold()
        if threshold_hours and not self.force:
            last = await self.db.last_successful_run(self.service)
            if last and last.get("started_at"):
                last_dt = datetime.fromisoformat(last["started_at"])
                gap = datetime.now(timezone.utc) - last_dt
                if gap < timedelta(hours=threshold_hours):
                    self.state.should_run = False
                    self.state.skip_reason = (
                        f"gap={gap.total_seconds()/3600:.1f}h < threshold={threshold_hours}h"
                    )
                    self.state.run_id = await self.db.insert_run(
                        service=self.service, status="skipped",
                        error=self.state.skip_reason)
                    return self

        # 4. 计算回溯窗口
        self.state.lookback_hours = self._compute_lookback()

        # 5. 注册 service_runs（status=running）
        self.state.run_id = await self.db.insert_run(
            service=self.service,
            status="running",
            lookback_hours=self.state.lookback_hours,
        )

        # 6. 开启 LLM usage 累加（business 期间 _call_cli / _call_api 会 push）
        from sentinel.runtime.llm_usage import begin_collect
        self._llm_acc = begin_collect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        # 关 caffeinate
        if self._caffeinate_proc:
            try:
                self._caffeinate_proc.send_signal(signal.SIGTERM)
            except Exception:
                pass

        # 关闭 LLM accumulator + 聚合
        from sentinel.runtime.llm_usage import end_collect, totals
        llm_stats: dict = {}
        if self._llm_acc is not None:
            llm_stats = totals(self._llm_acc)
            end_collect()

        # 如果 skip 已经写 run 了（或副机 should_run=False 没写 run），不再 update
        if not self.state.should_run:
            return False

        # 业务有异常 → failed
        if exc_type is not None:
            await self.db.update_run(
                self.state.run_id,
                status="failed",
                error=f"{exc_type.__name__}: {exc_val}",
                **self.state.stats,
                **llm_stats,
            )
            return False  # propagate

        # 正常完成
        await self.db.update_run(
            self.state.run_id,
            status="success",
            **self.state.stats,
            **llm_stats,
        )
        return False

    def _get_threshold(self) -> int | None:
        """从 config 取该 service 的 threshold_hours，无则返回 None（不守门）。"""
        svc_cfg = getattr(self.config.services, self.service, None)
        if svc_cfg is None:
            return None
        return getattr(svc_cfg, "threshold_hours", None)

    def _compute_lookback(self) -> float | None:
        """compute 回溯窗口（基于上次成功 run 时间 + max 封顶）。仅 collect 用。"""
        svc_cfg = getattr(self.config.services, self.service, None)
        if svc_cfg is None:
            return None
        max_hours = getattr(svc_cfg, "lookback_max_hours", None)
        if max_hours is None:
            return None
        # 简化：直接返回 max。实际可基于 last_successful 算 gap+1h 取 min。
        return float(max_hours)
