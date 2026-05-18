"""pytest fixtures."""
from __future__ import annotations

import pytest

from sentinel.db import Database


@pytest.fixture
async def tmp_db(tmp_path) -> Database:
    """临时 SQLite，每个测试独立。"""
    db = Database(str(tmp_path / "test.db"))
    await db.init_schema()
    return db
