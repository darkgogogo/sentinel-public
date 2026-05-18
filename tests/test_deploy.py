"""deploy module · plist 渲染 + host check 测试。

不真 launchctl load（避免污染本机），只测纯函数。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sentinel import deploy
from sentinel.deploy import (
    _render_plist, _local_hostname, get_deploy_host, check_host_match,
)


def test_local_hostname_returns_nonempty():
    h = _local_hostname()
    assert h and isinstance(h, str)


def test_render_plist_substitutes_placeholders(tmp_path):
    tmpl = tmp_path / "x.plist.template"
    tmpl.write_text(
        "Python={PYTHON_PATH}\nHome={SENTINEL_HOME}\nHomeShort={HOME}\n")
    rendered = _render_plist(tmpl)
    assert "{PYTHON_PATH}" not in rendered
    assert "{SENTINEL_HOME}" not in rendered
    assert "{HOME}" not in rendered
    assert ".venv/bin/python" in rendered
    assert "sentinel-v2" in rendered


def test_get_deploy_host_returns_none_when_no_file(monkeypatch, tmp_path):
    fake_host = tmp_path / ".host-missing"
    monkeypatch.setattr("sentinel.deploy.HOST_FILE", fake_host)
    assert get_deploy_host() is None


def test_get_deploy_host_reads_file(monkeypatch, tmp_path):
    fake = tmp_path / ".host"
    fake.write_text("Dark\n")
    monkeypatch.setattr("sentinel.deploy.HOST_FILE", fake)
    assert get_deploy_host() == "Dark"


def test_check_host_match_returns_true_when_no_host_file(
    monkeypatch, tmp_path,
):
    """未部署时，所有机器都允许跑（默认行为）。"""
    monkeypatch.setattr("sentinel.deploy.HOST_FILE", tmp_path / "nope")
    is_main, deploy_host = check_host_match()
    assert is_main is True
    assert deploy_host is None


def test_check_host_match_main_machine(monkeypatch, tmp_path):
    fake = tmp_path / ".host"
    fake.write_text("Dark\n")
    monkeypatch.setattr("sentinel.deploy.HOST_FILE", fake)
    monkeypatch.setattr("sentinel.deploy._local_hostname", lambda: "Dark")
    is_main, host = check_host_match()
    assert is_main is True
    assert host == "Dark"


def test_check_host_match_副机_rejected(monkeypatch, tmp_path):
    fake = tmp_path / ".host"
    fake.write_text("Dark\n")
    monkeypatch.setattr("sentinel.deploy.HOST_FILE", fake)
    monkeypatch.setattr("sentinel.deploy._local_hostname",
                       lambda: "OtherMac")
    is_main, host = check_host_match()
    assert is_main is False
    assert host == "Dark"


def test_all_plist_templates_exist():
    """5 个模板都得在。"""
    for label in deploy.PLIST_LABELS:
        path = deploy.LAUNCHD_TEMPLATES / f"{label}.plist.template"
        assert path.exists(), f"模板缺失: {path}"


def test_plist_template_renders_valid_xml():
    """模板渲染后应是有效 plist XML。"""
    import xml.etree.ElementTree as ET
    for label in deploy.PLIST_LABELS:
        tmpl_path = deploy.LAUNCHD_TEMPLATES / f"{label}.plist.template"
        rendered = _render_plist(tmpl_path)
        # XML 解析应该不报错
        try:
            ET.fromstring(rendered)
        except ET.ParseError as e:
            pytest.fail(f"{label} 渲染后 XML 无效: {e}")
        # 含 Label key
        assert label in rendered


# ===== shell host check 集成测试 =====


async def test_shell_skips_when_副机(tmp_db, monkeypatch, tmp_path):
    """ServiceShell 在副机应 should_run=False，并写一行 skipped 用于排错。"""
    import aiosqlite
    from sentinel.config import Config
    from sentinel.runtime.shell import ServiceShell

    fake_host = tmp_path / ".host"
    fake_host.write_text("MainMachine\n")
    monkeypatch.setattr("sentinel.deploy.HOST_FILE", fake_host)
    monkeypatch.setattr("sentinel.deploy._local_hostname",
                       lambda: "SubMachine")

    config = Config()
    config.host_check_enabled = True
    async with ServiceShell("collect", config, tmp_db) as shell:
        assert shell.should_run is False
        assert "host_mismatch" in (shell.state.skip_reason or "")

    # last_successful_run 仍 None（写的是 skipped 不是 success）
    last = await tmp_db.last_successful_run("collect")
    assert last is None

    # service_runs 应有一行 skipped + host_mismatch error
    async with aiosqlite.connect(tmp_db.db_path) as raw:
        raw.row_factory = aiosqlite.Row
        async with raw.execute(
            "SELECT service, status, error FROM service_runs "
            "ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["service"] == "collect"
        assert row["status"] == "skipped"
        assert "host_mismatch" in row["error"]
        assert "deploy=MainMachine" in row["error"]
        assert "local=SubMachine" in row["error"]


async def test_shell_runs_when_host_check_disabled(
    tmp_db, monkeypatch, tmp_path,
):
    """config.host_check_enabled=False → 跳过 host check。"""
    from sentinel.config import Config
    from sentinel.runtime.shell import ServiceShell

    fake_host = tmp_path / ".host"
    fake_host.write_text("MainMachine\n")
    monkeypatch.setattr("sentinel.deploy.HOST_FILE", fake_host)
    monkeypatch.setattr("sentinel.deploy._local_hostname",
                       lambda: "SubMachine")

    config = Config()
    config.host_check_enabled = False
    async with ServiceShell("collect", config, tmp_db) as shell:
        assert shell.should_run is True


async def test_shell_runs_on_main_machine(tmp_db, monkeypatch, tmp_path):
    from sentinel.config import Config
    from sentinel.runtime.shell import ServiceShell

    fake_host = tmp_path / ".host"
    fake_host.write_text("Dark\n")
    monkeypatch.setattr("sentinel.deploy.HOST_FILE", fake_host)
    monkeypatch.setattr("sentinel.deploy._local_hostname", lambda: "Dark")

    config = Config()
    async with ServiceShell("collect", config, tmp_db) as shell:
        assert shell.should_run is True
