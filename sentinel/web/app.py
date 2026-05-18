"""FastAPI 主入口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sentinel.config import Config, load_config, CONFIG_PATH, ENV_PATH, DB_PATH
from sentinel.db import Database
from sentinel.web.routes import register_routes


WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def build_app(config: Config | None = None,
              db_path: str | Path | None = None) -> FastAPI:
    """构造 FastAPI 实例 · 可测试时注入 config / db_path。"""
    if config is None:
        config = load_config(CONFIG_PATH, ENV_PATH)
    db_path = str(db_path or DB_PATH)

    app = FastAPI(
        title="Sentinel v2",
        description="通用主题舆情雷达 · 只读 dashboard (D1)",
        version="2.0.0",
        docs_url=None,
        redoc_url=None,
    )

    # 注入 deps 到 app state
    app.state.config = config
    app.state.db = Database(db_path)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # 注入 static asset version (CSS mtime) 用作 cache-buster
    css_path = STATIC_DIR / "style.css"
    static_v = str(int(css_path.stat().st_mtime)) if css_path.exists() else "0"
    templates.env.globals["static_v"] = static_v
    app.state.templates = templates

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    register_routes(app)
    return app
