"""Dashboard REST API。

所有端点都挂在 /api 下，直接复用 InternalSource 的方法读写 internal.db。
不依赖 config.yaml——db 路径由 CLI --db 传入（存在 app.state），agent_label 默认 "dashboard"。

InternalSource 需要 binding（读 internal_db / agent_label），这里用一个最小
RepoBinding 实例承载，profile/source 等字段对 dashboard 无意义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import RepoBinding
from ..sources.internal import STATUSES, InternalSource

router = APIRouter(prefix="/api")


@dataclass
class DashboardCtx:
    db_path: str
    agent_label: str


def _ctx(request: Request) -> DashboardCtx:
    """从 app.state 取 db 路径与 agent_label（由 create_app 注入）。"""
    return DashboardCtx(
        db_path=request.app.state.db_path,
        agent_label=request.app.state.agent_label,
    )


def _source(ctx: DashboardCtx) -> InternalSource:
    """构造一个指向指定 db 的 InternalSource。每次请求新建，连接轻量。"""
    binding = RepoBinding(
        repo="",
        profile="",
        source="internal",
        agent_label=ctx.agent_label or "dashboard",
        internal_db=ctx.db_path,
    )
    return InternalSource(binding=binding)


# ── 请求体模型 ────────────────────────────────────────────────────────


class CreateIssueReq(BaseModel):
    title: str
    body: str = ""
    author: str = ""
    actor_type: str = Field(default="human", pattern="^(human|agent)$")
    kind: str = Field(default="issue", pattern="^(issue|pr)$")
    labels: list[str] = Field(default_factory=list)


class AddCommentReq(BaseModel):
    body: str
    author: str = ""
    actor_type: str = Field(default="human", pattern="^(human|agent)$")


class MoveReq(BaseModel):
    to_status: str
    actor: str = ""
    actor_type: str = Field(default="human", pattern="^(human|agent)$")
    comment: str = ""


class CloseReq(BaseModel):
    actor: str = ""
    actor_type: str = Field(default="human", pattern="^(human|agent)$")


# ── 响应工具 ──────────────────────────────────────────────────────────


def _resource_to_dict(r) -> dict[str, Any]:
    return {
        "kind": r.kind,
        "number": r.number,
        "title": r.title,
        "body": r.body,
        "state": r.state,
        "status": r.status,
        "labels": list(r.labels),
        "author": r.author,
        "actor_type": r.actor_type,
        "assignee": r.assignee,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


def _comment_to_dict(c) -> dict[str, Any]:
    return {
        "id": c.id,
        "author": c.author,
        "body": c.body,
        "created_at": c.created_at,
    }


# ── 端点 ──────────────────────────────────────────────────────────────


@router.get("/statuses")
def list_statuses() -> list[str]:
    """看板状态列顺序。"""
    return list(STATUSES)


@router.get("/team")
def list_team(ctx: DashboardCtx = Depends(_ctx)) -> list[dict[str, Any]]:
    """列出团队成员（agent 花名册 + 自我介绍）。读 db 的 projects 表。"""
    src = _source(ctx)
    return [
        {
            "project": m["name"],
            "agent_label": m["agent_label"],
            "cwd": m["cwd"],
            "intro": m["intro"],
        }
        for m in src.list_projects_meta()
    ]


@router.get("/projects")
def list_projects(ctx: DashboardCtx = Depends(_ctx)) -> list[dict[str, Any]]:
    """列出所有项目及其 issue 计数（0 issue 项目也显示，读 projects 表）。"""
    src = _source(ctx)
    return src.list_projects_with_counts()


@router.get("/projects/{project}/issues")
def list_issues(
    project: str,
    ctx: DashboardCtx = Depends(_ctx),
    kind: str = "issue",
) -> list[dict[str, Any]]:
    """列出某项目的全部 issue（任意状态），给看板用。"""
    if kind not in ("issue", "pr"):
        raise HTTPException(status_code=400, detail="kind 只能是 issue 或 pr")
    src = _source(ctx)
    return [_resource_to_dict(r) for r in src.list_all(project, [kind])]


@router.get("/projects/{project}/issues/{number}")
def get_issue(
    project: str,
    number: int,
    ctx: DashboardCtx = Depends(_ctx),
    kind: str = "issue",
) -> dict[str, Any]:
    """issue 详情：本体 + 评论 + 状态历史。"""
    if kind not in ("issue", "pr"):
        raise HTTPException(status_code=400, detail="kind 只能是 issue 或 pr")
    src = _source(ctx)
    res = src.get_issue(project, kind, number)
    if res is None:
        raise HTTPException(status_code=404, detail=f"找不到 {kind} #{number}（项目 {project}）")
    comments = src.list_comments(project, res)
    history = src.list_status_history(project, res)
    return {
        **_resource_to_dict(res),
        "comments": [_comment_to_dict(c) for c in comments],
        "history": history,
    }


@router.post("/projects/{project}/issues")
def create_issue(
    project: str,
    req: CreateIssueReq,
    ctx: DashboardCtx = Depends(_ctx),
) -> dict[str, Any]:
    """提一个新 issue/PR。"""
    src = _source(ctx)
    author = req.author or ("anonymous" if req.actor_type == "human" else "anonymous-agent")
    res = src.create_issue(
        repo=project, kind=req.kind,
        title=req.title, body=req.body,
        author=author, actor_type=req.actor_type,
        labels=req.labels,
    )
    return _resource_to_dict(res)


@router.post("/projects/{project}/issues/{number}/comments")
def add_comment(
    project: str,
    number: int,
    req: AddCommentReq,
    ctx: DashboardCtx = Depends(_ctx),
    kind: str = "issue",
) -> dict[str, Any]:
    if kind not in ("issue", "pr"):
        raise HTTPException(status_code=400, detail="kind 只能是 issue 或 pr")
    src = _source(ctx)
    res = src.get_issue(project, kind, number)
    if res is None:
        raise HTTPException(status_code=404, detail=f"找不到 {kind} #{number}")
    c = src.add_comment(
        repo=project, resource=res,
        body=req.body, author=req.author or "anonymous",
        actor_type=req.actor_type,
    )
    return _comment_to_dict(c)


@router.post("/projects/{project}/issues/{number}/move")
def move_issue(
    project: str,
    number: int,
    req: MoveReq,
    ctx: DashboardCtx = Depends(_ctx),
    kind: str = "issue",
) -> dict[str, Any]:
    """改状态。返回新的 issue 快照。"""
    if req.to_status not in STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"非法 status: {req.to_status}，合法值: {list(STATUSES)}",
        )
    if kind not in ("issue", "pr"):
        raise HTTPException(status_code=400, detail="kind 只能是 issue 或 pr")
    src = _source(ctx)
    res = src.get_issue(project, kind, number)
    if res is None:
        raise HTTPException(status_code=404, detail=f"找不到 {kind} #{number}")
    src.move_status(
        repo=project, resource=res, to_status=req.to_status,
        actor=req.actor or ctx.agent_label, actor_type=req.actor_type,
        comment=req.comment,
    )
    fresh = src.get_issue(project, kind, number)
    return _resource_to_dict(fresh) if fresh else _resource_to_dict(res)


@router.post("/projects/{project}/issues/{number}/close")
def close_issue(
    project: str,
    number: int,
    req: CloseReq,
    ctx: DashboardCtx = Depends(_ctx),
    kind: str = "issue",
) -> dict[str, Any]:
    if kind not in ("issue", "pr"):
        raise HTTPException(status_code=400, detail="kind 只能是 issue 或 pr")
    src = _source(ctx)
    res = src.get_issue(project, kind, number)
    if res is None:
        raise HTTPException(status_code=404, detail=f"找不到 {kind} #{number}")
    src.close_issue(
        project, res,
        actor=req.actor or ctx.agent_label, actor_type=req.actor_type,
    )
    fresh = src.get_issue(project, kind, number)
    return _resource_to_dict(fresh) if fresh else _resource_to_dict(res)
