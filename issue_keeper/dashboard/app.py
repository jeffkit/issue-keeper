"""FastAPI 应用工厂与启动入口。

- 挂载 /api 路由（来自 api.py）
- 托管前端构建产物 frontend/dist/（若存在）
- 开发模式：前端单独 `npm run dev`，后端开 CORS 允许 5173 端口
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router

# 仓库根 = issue_keeper/dashboard/app.py 往上两级
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DIST_DIR = _REPO_ROOT / "frontend" / "dist"


def _frontend_dist() -> Path | None:
    """返回前端构建产物目录，不存在则 None。"""
    if _DIST_DIR.is_dir() and (_DIST_DIR / "index.html").exists():
        return _DIST_DIR
    return None


def create_app(db_path: str, *, agent_label: str = "dashboard",
               team_path: str | None = None) -> FastAPI:
    """构造 FastAPI 应用。

    db_path: internal.db 路径
    agent_label: dashboard 以谁的身份发评论/改状态（默认 "dashboard"）
    team_path: team.json 路径（团队成员花名册 + 介绍，默认 ~/.issue-keeper/team.json）
    """
    app = FastAPI(title="issue-keeper dashboard", version="0.1.0")

    # 开发模式：前端 Vite 跑在 5173，允许跨域打到后端 API
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # db / agent_label / team_path 通过 app.state 传给路由（api.py 的 _ctx 依赖读取）
    app.state.db_path = db_path
    app.state.agent_label = agent_label
    app.state.team_path = team_path
    app.include_router(router)

    dist = _frontend_dist()
    if dist is None:
        @app.get("/")
        def _index_no_frontend() -> JSONResponse:
            return JSONResponse(
                {
                    "message": "前端未构建。请在 frontend/ 下运行 `npm install && npm run build`。",
                    "api": "/api",
                    "dist_expected": str(_DIST_DIR),
                },
                status_code=200,
            )
        return app

    # 托管 Vite 构建产物：assets/ 静态，其余路径回 index.html（SPA fallback）
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    def _index() -> FileResponse:
        return FileResponse(str(dist / "index.html"))

    @app.get("/{full_path:path}")
    def _spa_fallback(full_path: str) -> FileResponse:
        # 非静态资源、非 /api 路径回 index.html（前端路由）
        if full_path.startswith("api/") or full_path.startswith("assets/"):
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        candidate = dist / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(dist / "index.html"))

    return app


def run_dashboard(db_path: str, *, host: str = "127.0.0.1",
                  port: int = 7433, agent_label: str = "dashboard",
                  team_path: str | None = None) -> None:
    """启动 dashboard 服务（阻塞）。"""
    import uvicorn

    app = create_app(db_path, agent_label=agent_label, team_path=team_path)
    print(f"issue-keeper dashboard 启动: http://{host}:{port}")
    print(f"  db: {db_path}")
    print(f"  前端: {'已构建 (frontend/dist)' if _frontend_dist() else '未构建，仅 API 可用'}")
    uvicorn.run(app, host=host, port=port, log_level="info")
