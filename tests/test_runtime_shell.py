"""ServiceShell · pause 检查 / 阈值守门 / context manager 行为。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sentinel.config import Config
from sentinel.runtime.shell import (
    ServiceShell, check_pause, PAUSE_FILE_PREFIX,
)


def _config() -> Config:
    return Config()


def test_check_pause_no_file(tmp_path):
    paused, exp = check_pause("collect", project_root=tmp_path)
    assert paused is False and exp is None


def test_check_pause_indefinite(tmp_path):
    (tmp_path / f"{PAUSE_FILE_PREFIX}collect").write_text("")
    paused, exp = check_pause("collect", project_root=tmp_path)
    assert paused is True and exp is None


def test_check_pause_expired_auto_removes(tmp_path):
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    pf = tmp_path / f"{PAUSE_FILE_PREFIX}collect"
    pf.write_text(f"expire_at={expired}\n")
    paused, exp = check_pause("collect", project_root=tmp_path)
    assert paused is False
    assert not pf.exists()  # 自动 rm


def test_check_pause_active_kept(tmp_path):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    pf = tmp_path / f"{PAUSE_FILE_PREFIX}collect"
    pf.write_text(f"expire_at={future}\n")
    paused, exp = check_pause("collect", project_root=tmp_path)
    assert paused is True
    assert exp == future
    assert pf.exists()


async def test_shell_normal_run_records_success(tmp_db):
    async with ServiceShell("collect", _config(), tmp_db) as shell:
        assert shell.should_run
        shell.record(messages_collected=7)
    last = await tmp_db.last_successful_run("collect")
    assert last is not None
    assert last["messages_collected"] == 7


async def test_shell_threshold_gates_second_run(tmp_db):
    """第一次跑完后立刻第二次：应被 12h 阈值挡下。"""
    async with ServiceShell("collect", _config(), tmp_db) as shell:
        shell.record(messages_collected=1)
    async with ServiceShell("collect", _config(), tmp_db) as shell2:
        assert shell2.should_run is False
        assert "threshold" in (shell2.state.skip_reason or "")


async def test_shell_force_bypasses_threshold(tmp_db):
    async with ServiceShell("collect", _config(), tmp_db) as shell:
        shell.record(messages_collected=1)
    async with ServiceShell("collect", _config(), tmp_db, force=True) as shell2:
        assert shell2.should_run is True


async def test_shell_exception_records_failed(tmp_db):
    with pytest.raises(ValueError):
        async with ServiceShell("collect", _config(), tmp_db) as shell:
            assert shell.should_run
            raise ValueError("boom")
    # 验证 run 状态 failed
    import aiosqlite
    async with aiosqlite.connect(tmp_db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM service_runs WHERE service='collect' "
                "ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
    assert row["status"] == "failed"
    assert "boom" in (row["error"] or "")


async def test_shell_records_llm_usage_to_run(tmp_db):
    """ServiceShell 应把 LLM usage 累加器的总值写入 service_runs。"""
    import aiosqlite
    from sentinel.runtime.llm_usage import record_call

    async with ServiceShell("alert", _config(), tmp_db) as shell:
        assert shell.should_run
        # 模拟两次 LLM 调用各自上报 usage
        record_call({
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cost_usd": 0.0012,
        })
        record_call({
            "input_tokens": 80, "output_tokens": 30,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 100,
            "cost_usd": 0.0008,
        })

    async with aiosqlite.connect(tmp_db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT llm_tokens_in, llm_tokens_out, llm_cache_read_tokens, "
            "       llm_cache_creation_tokens, llm_cost_usd "
            "FROM service_runs WHERE service='alert' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    assert row["llm_tokens_in"] == 180
    assert row["llm_tokens_out"] == 80
    assert row["llm_cache_read_tokens"] == 250
    assert row["llm_cache_creation_tokens"] == 100
    assert abs(row["llm_cost_usd"] - 0.002) < 1e-6


async def test_shell_no_llm_calls_keeps_zero(tmp_db):
    """无 LLM 调用时 llm_* 列默认 0。"""
    import aiosqlite
    async with ServiceShell("collect", _config(), tmp_db) as shell:
        shell.record(messages_collected=3)
    async with aiosqlite.connect(tmp_db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT llm_tokens_in, llm_cost_usd "
            "FROM service_runs WHERE service='collect' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    assert row["llm_tokens_in"] == 0
    assert row["llm_cost_usd"] == 0.0
