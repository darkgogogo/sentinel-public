"""Sentinel v2 配置加载。

config.yaml 主配置 + secrets/.env 密钥，分轨。

路径覆盖（env var · 优先级 > 默认）：
- SENTINEL_HOME    — 项目根（默认 ~/sentinel-v2）
- SENTINEL_KB_ROOT — 报告/告警/主题写入目标（默认 ~/Documents/Sentinel）
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field


def _detect_claude_cli() -> str:
    """启动时 `which claude` 自动探测；找不到返回空串（用户须在 config.yaml 显式填）。"""
    found = shutil.which("claude")
    return found or ""


class LLMConfig(BaseModel):
    provider: str = "cli"
    # 留空时启动会自动 `which claude` 探测；config.yaml 中显式设值会覆盖
    cli_path: str = ""
    cli_timeout: int = 600


class ModelsConfig(BaseModel):
    triage: str = "claude-haiku-4-5"
    deep_analysis: str = "claude-opus-4-7"
    advisor: str = "claude-haiku-4-5"


class CollectServiceConfig(BaseModel):
    threshold_hours: int = 12
    lookback_max_hours: int = 48
    retention_days: int = 90
    source_failure_limit: int = 5
    service_runs_retention_days: int = 365  # service_runs 表保留窗口（默认 1 年）


class AlertServiceConfig(BaseModel):
    threshold_hours: int = 12
    triage_max_messages: int = 200
    dedupe_hours: int = 24


class AnalyzeServiceConfig(BaseModel):
    default_period_hours: int = 168


class AdvisorServiceConfig(BaseModel):
    feedback_window_days: int = 30


class ServicesConfig(BaseModel):
    collect: CollectServiceConfig = Field(default_factory=CollectServiceConfig)
    alert: AlertServiceConfig = Field(default_factory=AlertServiceConfig)
    analyze: AnalyzeServiceConfig = Field(default_factory=AnalyzeServiceConfig)
    advisor: AdvisorServiceConfig = Field(default_factory=AdvisorServiceConfig)


class PushConfig(BaseModel):
    channels: list[str] = Field(default_factory=lambda: ["telegram"])


class Config(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    services: ServicesConfig = Field(default_factory=ServicesConfig)
    push: PushConfig = Field(default_factory=PushConfig)
    host_check_enabled: bool = True
    secrets: dict[str, str] = Field(default_factory=dict)


def load_config(yaml_path: str | Path, env_path: str | Path) -> Config:
    """加载 config.yaml + .env，合并成 Config 对象。cli_path 空时自动探测。"""
    yaml_data = {}
    yp = Path(yaml_path)
    if yp.exists():
        yaml_data = yaml.safe_load(yp.read_text()) or {}

    secrets = {}
    ep = Path(env_path)
    if ep.exists():
        secrets = {k: v for k, v in dotenv_values(str(ep)).items() if v is not None}

    yaml_data["secrets"] = secrets
    cfg = Config(**yaml_data)

    # cli_path 留空 → which claude 自动探测
    if cfg.llm.provider == "cli" and not cfg.llm.cli_path:
        cfg.llm.cli_path = _detect_claude_cli()
    return cfg


def _detect_project_root() -> Path:
    """项目根 · 优先级 env SENTINEL_HOME > package 安装位置自动 detect > ~/sentinel-v2。"""
    env_home = os.environ.get("SENTINEL_HOME")
    if env_home:
        return Path(env_home)
    # 从 sentinel/config.py 反推：<project>/sentinel/config.py → <project>/
    pkg_root = Path(__file__).resolve().parent.parent
    if (pkg_root / "pyproject.toml").exists():
        return pkg_root
    return Path.home() / "sentinel-v2"


PROJECT_ROOT = _detect_project_root()
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / "secrets" / ".env"
DB_PATH = PROJECT_ROOT / "db" / "messages.db"

# KB 路径（报告/告警/主题写入目标）· SENTINEL_KB_ROOT 覆盖默认 ~/Documents/Sentinel
KB_ROOT = Path(os.environ.get("SENTINEL_KB_ROOT", str(Path.home() / "Documents" / "Sentinel")))
