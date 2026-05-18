"""deploy · launchd plist 部署 + host check。

CLI 入口：
    python -m sentinel deploy install     渲染 plist 到 ~/Library/LaunchAgents/ + launchctl load
    python -m sentinel deploy uninstall   launchctl unload + 移除 plist
    python -m sentinel deploy status      看哪些 plist 已部署 + 进程状态

host check：
    .host 文件在 ~/.kb/.../Sentinel/00-系统设计/.host
    runtime.shell 启动时检查；副机 LocalHostName != .host 内容则友好退出
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from sentinel.config import KB_ROOT, PROJECT_ROOT


log = logging.getLogger(__name__)

LAUNCHD_TEMPLATES = PROJECT_ROOT / "launchd"
LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
HOST_FILE = KB_ROOT / "00-系统设计" / ".host"

PLIST_LABELS = [
    "local.sentinel.collect",
    "local.sentinel.alert",
    "local.sentinel.weekly",
    "local.sentinel.backup",
    "local.sentinel.watchdog",
    "local.sentinel.web",        # Web UI 长驻服务（开机自启 + KeepAlive）
]


def _local_hostname() -> str:
    """LocalHostName · 取本机稳定标识。

    踩坑：launchd 启动子进程 PATH 不含 /usr/sbin → `scutil` 找不到 →
    fallback 到 socket.gethostname() → mDNS 在网络变动时返回 'Mac.lan' 等。
    解法：用绝对路径 + socket fallback 剥常见 mDNS 后缀 (.local / .lan / .home)。
    """
    # 优先用 scutil（绝对路径，避免 launchd PATH 问题）
    for path in ("/usr/sbin/scutil", "scutil"):
        try:
            out = subprocess.check_output(
                [path, "--get", "LocalHostName"], text=True).strip()
            if out:
                return out
        except (FileNotFoundError, subprocess.CalledProcessError, OSError):
            continue
    # fallback: socket + 剥 mDNS 后缀
    import socket
    name = socket.gethostname()
    for suffix in (".local", ".lan", ".home", ".localdomain"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def get_deploy_host() -> str | None:
    """读 .host 文件 · 不存在返回 None。"""
    if not HOST_FILE.exists():
        return None
    return HOST_FILE.read_text().strip() or None


def check_host_match() -> tuple[bool, str | None]:
    """副机 check：本机 hostname 与 .host 一致才允许跑 service。

    Returns (is_main_host, deploy_host)。
    .host 不存在 → (True, None) 允许跑（未部署前的默认状态）。
    """
    deploy = get_deploy_host()
    if deploy is None:
        return True, None
    return _local_hostname() == deploy, deploy


def _render_plist(template_path: Path) -> str:
    """读 plist 模板 + 替换占位符。"""
    text = template_path.read_text(encoding="utf-8")
    home = str(Path.home())
    python_path = str(PROJECT_ROOT / ".venv" / "bin" / "python")
    replacements = {
        "{PYTHON_PATH}": python_path,
        "{SENTINEL_HOME}": str(PROJECT_ROOT),
        "{HOME}": home,
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


def _launchctl(action: str, target: str) -> int:
    """跑 launchctl <action> <target>，返回 returncode。"""
    try:
        proc = subprocess.run(
            ["launchctl", action, target],
            capture_output=True, text=True)
        if proc.returncode != 0 and proc.stderr:
            log.warning("launchctl %s %s 失败: %s",
                        action, target, proc.stderr.strip())
        return proc.returncode
    except Exception as e:
        log.error("launchctl 异常: %s", e)
        return -1


def install() -> dict:
    """渲染所有 plist 模板到 ~/Library/LaunchAgents/ + launchctl load。

    同时写 .host 文件标识本机为部署主机。
    """
    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    HOST_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 写 .host
    hostname = _local_hostname()
    HOST_FILE.write_text(hostname + "\n")
    print(f"✓ .host 已写：{hostname} → {HOST_FILE}")

    # 渲染 + load 每个 plist
    installed = []
    failed = []
    for label in PLIST_LABELS:
        tmpl_path = LAUNCHD_TEMPLATES / f"{label}.plist.template"
        if not tmpl_path.exists():
            failed.append((label, f"模板不存在: {tmpl_path}"))
            continue
        target = LAUNCHD_DIR / f"{label}.plist"
        # 已存在先 unload
        if target.exists():
            _launchctl("unload", str(target))
        # 渲染
        rendered = _render_plist(tmpl_path)
        target.write_text(rendered)
        # load
        rc = _launchctl("load", str(target))
        if rc == 0:
            installed.append(label)
            print(f"✓ load {label}")
        else:
            failed.append((label, f"launchctl load rc={rc}"))
            print(f"✗ load {label} (rc={rc})")

    return {
        "host": hostname,
        "installed": installed,
        "failed": failed,
    }


def uninstall() -> dict:
    """unload + 删 plist 文件。不动 .host（保留作"曾在哪部署"记录）。"""
    removed = []
    for label in PLIST_LABELS:
        target = LAUNCHD_DIR / f"{label}.plist"
        if not target.exists():
            continue
        _launchctl("unload", str(target))
        target.unlink()
        removed.append(label)
        print(f"✓ unload + 移除 {label}")
    return {"removed": removed}


def status() -> dict:
    """看哪些 plist 已部署 · 哪些有进程在跑。"""
    deployed = []
    proc_map = _launchctl_list()
    for label in PLIST_LABELS:
        target = LAUNCHD_DIR / f"{label}.plist"
        info = {
            "label": label,
            "plist_exists": target.exists(),
            "loaded": label in proc_map,
            "last_status": proc_map.get(label),
        }
        deployed.append(info)
    return {
        "host_file_exists": HOST_FILE.exists(),
        "deploy_host": get_deploy_host(),
        "local_hostname": _local_hostname(),
        "is_main_host": check_host_match()[0],
        "services": deployed,
    }


def _launchctl_list() -> dict[str, dict]:
    """parse `launchctl list | grep sentinel`。"""
    try:
        out = subprocess.check_output(["launchctl", "list"], text=True)
    except Exception:
        return {}
    result = {}
    for line in out.split("\n"):
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        label = parts[2].strip()
        if label.startswith("local.sentinel"):
            result[label] = {
                "pid": parts[0].strip(),
                "last_exit": parts[1].strip(),
            }
    return result
