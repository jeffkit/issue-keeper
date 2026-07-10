"""gh CLI 封装：列出 issue / 读取评论 / 发评论。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


def _run_gh(args: list[str]) -> str:
    """运行 gh 子命令，返回 stdout 文本。失败抛 RuntimeError。"""
    cmd = ["gh", *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh 命令失败 ({' '.join(cmd)}): exit={proc.returncode}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    return proc.stdout


@dataclass
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    author: str
    created_at: str
    updated_at: str


@dataclass
class Comment:
    id: str  # GraphQL node ID（稳定唯一，用作去重 key）
    url: str  # 评论完整 URL（含 #issuecomment-<dbid>）
    author: str
    body: str
    created_at: str


def list_open_issues(repo: str, labels: list[str] | None = None) -> list[Issue]:
    """列出 open issue。labels 非空时按 label 过滤。"""
    args = [
        "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,body,state,labels,author,createdAt,updatedAt",
        "--limit", "100",
    ]
    if labels:
        for lb in labels:
            args += ["--label", lb]
    out = _run_gh(args)
    rows: list[dict[str, Any]] = json.loads(out) if out.strip() else []
    issues: list[Issue] = []
    for r in rows:
        labels_list = [l.get("name", "") if isinstance(l, dict) else str(l) for l in (r.get("labels") or [])]
        author = r.get("author") or {}
        author_login = author.get("login", "") if isinstance(author, dict) else str(author)
        issues.append(
            Issue(
                number=int(r["number"]),
                title=r.get("title", "") or "",
                body=r.get("body", "") or "",
                state=r.get("state", "OPEN"),
                labels=labels_list,
                author=author_login,
                created_at=r.get("createdAt", ""),
                updated_at=r.get("updatedAt", ""),
            )
        )
    return issues


def list_comments(repo: str, issue_number: int) -> list[Comment]:
    """读取某 issue 的全部评论（按时间升序）。"""
    out = _run_gh([
        "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "comments",
    ])
    data = json.loads(out) if out.strip() else {}
    raw_comments = data.get("comments") or []
    comments: list[Comment] = []
    for c in raw_comments:
        author = c.get("author") or {}
        author_login = author.get("login", "") if isinstance(author, dict) else str(author)
        comments.append(
            Comment(
                id=str(c.get("id", "")),
                url=c.get("url", "") or "",
                author=author_login,
                body=c.get("body", "") or "",
                created_at=c.get("createdAt", ""),
            )
        )
    # gh 返回评论通常已按时间升序，这里再保险按 node id 稳定排一下
    comments.sort(key=lambda c: c.id)
    return comments


def post_comment(repo: str, issue_number: int, body: str) -> None:
    """在 issue 上发表评论。"""
    _run_gh([
        "issue", "comment", str(issue_number),
        "--repo", repo,
        "--body", body,
    ])


def whoami() -> str:
    """返回当前 gh 登录用户名，用于忽略 bot 自己发的评论。"""
    try:
        return _run_gh(["api", "user", "--jq", ".login"]).strip()
    except RuntimeError:
        return ""
