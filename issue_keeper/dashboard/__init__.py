"""Web 看板（dashboard）。

后端：FastAPI，直接复用 InternalSource 读写 internal.db。
前端：Vite + React 工程构建到 frontend/dist/，由 FastAPI 托管静态文件。

启动：
    python -m issue_keeper dashboard --port 7433 [--db PATH]

开发时前端单独 `npm run dev`（默认 5173 端口），通过 Vite 代理打到后端 API；
生产/本机使用时前端 `npm run build` 产出 dist/，后端一并托管。
"""

from __future__ import annotations

from .app import create_app, run_dashboard

__all__ = ["create_app", "run_dashboard"]
