"""GitHub 的 IssueSource 实现。

两种实现：
- GitHubSource      —— 用 gh CLI 访问，以 gh auth 登录账号身份。默认。
- GitHubTokenSource —— 用 PAT 直接调 GitHub REST API，可指定任意账号身份。
                       不依赖 gh CLI，支持分页（去掉 100 条限制）。

两种实现共用一套解析逻辑（GitHub REST API 与 gh CLI --json 输出结构基本一致）。
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Any

from . import Comment, IssueSource, Resource

GITHUB_API = "https://api.github.com"


def _parse_labels(raw: Any) -> list[str]:
    return [l.get("name", "") if isinstance(l, dict) else str(l) for l in (raw or [])]


def _parse_author(raw: Any) -> str:
    if isinstance(raw, dict):
        return raw.get("login", "")
    return str(raw) if raw else ""


def _row_to_resource(row: dict[str, Any], kind: str, repo: str) -> Resource:
    return Resource(
        kind=kind,
        number=int(row["number"]),
        title=row.get("title", "") or "",
        body=row.get("body", "") or "",
        state=row.get("state", "open"),
        labels=_parse_labels(row.get("labels")),
        author=_parse_author(row.get("user") or row.get("author")),
        created_at=row.get("created_at", ""),
        updated_at=row.get("updated_at", ""),
        source_ref=f"{repo}#{kind}-{row['number']}",
    )


def _gh_url(repo: str, resource: Resource) -> str:
    base = f"https://github.com/{repo}"
    path = "issues" if resource.kind == "issue" else "pull"
    return f"{base}/{path}/{resource.number}"


# ─────────────────────────────────────────────────────────────────────
# gh CLI 实现
# ─────────────────────────────────────────────────────────────────────


def _run_gh(args: list[str]) -> str:
    cmd = ["gh", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh 命令失败 ({' '.join(cmd)}): exit={proc.returncode}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    return proc.stdout


class GitHubSource(IssueSource):
    """基于 gh CLI 的 GitHub issue/PR 适配器。"""

    def __init__(self, *, list_limit: int = 100) -> None:
        self._limit = list_limit

    def list_open(
        self, repo: str, kinds: list[str], labels: list[str] | None = None
    ) -> list[Resource]:
        out: list[Resource] = []
        for kind in kinds:
            out.extend(self._list_one(repo, kind, labels))
        return out

    def _list_one(
        self, repo: str, kind: str, labels: list[str] | None
    ) -> list[Resource]:
        args = [
            kind, "list",
            "--repo", repo,
            "--state", "open",
            "--json", "number,title,body,state,labels,author,createdAt,updatedAt",
            "--limit", str(self._limit),
        ]
        if labels:
            for lb in labels:
                args += ["--label", lb]
        out = _run_gh(args)
        rows: list[dict[str, Any]] = json.loads(out) if out.strip() else []
        return [_row_to_resource(r, kind, repo) for r in rows]

    def list_comments(self, repo: str, resource: Resource) -> list[Comment]:
        out = _run_gh([
            resource.kind, "view", str(resource.number),
            "--repo", repo,
            "--json", "comments",
        ])
        data = json.loads(out) if out.strip() else {}
        raw_comments = data.get("comments") or []
        comments: list[Comment] = []
        for c in raw_comments:
            comments.append(
                Comment(
                    id=str(c.get("id", "")),
                    url=c.get("url", "") or "",
                    author=_parse_author(c.get("author")),
                    body=c.get("body", "") or "",
                    created_at=c.get("createdAt", ""),
                )
            )
        comments.sort(key=lambda c: c.id)
        return comments

    def post_comment(self, repo: str, resource: Resource, body: str) -> None:
        _run_gh([
            resource.kind, "comment", str(resource.number),
            "--repo", repo,
            "--body", body,
        ])

    @lru_cache(maxsize=1)
    def self_identity(self) -> str:
        try:
            return _run_gh(["api", "user", "--jq", ".login"]).strip()
        except RuntimeError:
            return ""

    def web_url(self, repo: str, resource: Resource) -> str:
        return _gh_url(repo, resource)


# ─────────────────────────────────────────────────────────────────────
# PAT token 实现（直接调 REST API）
# ─────────────────────────────────────────────────────────────────────


class GitHubTokenSource(IssueSource):
    """基于 PAT 的 GitHub REST API 适配器。

    用一个 Personal Access Token 调 GitHub REST API，以 token 所属账号身份读写。
    相比 gh CLI 模式：
    - 不依赖本地 gh CLI 已登录
    - 支持分页（默认每页 100，自动翻页）
    - 可指定任意账号身份（你自己的、bot 账号的、未来每项目一个 token）

    token 来源（按优先级）：
      1. binding.github_token 字段（支持 ${ENV_VAR} 展开）
      2. 环境变量 GITHUB_TOKEN
      3. 环境变量 GH_TOKEN
    """

    def __init__(self, binding) -> None:
        self._binding = binding
        token = getattr(binding, "github_token", None) or ""
        # 展开 ${VAR}
        token = self._expand_env(token)
        if not token:
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not token:
            raise ValueError(
                "github_token source 缺少 token：请在 binding 里配 github_token，"
                "或设置环境变量 GITHUB_TOKEN / GH_TOKEN"
            )
        self._token = token
        self._identity: str | None = None  # 懒加载

    @staticmethod
    def _expand_env(value: str) -> str:
        s = str(value)
        for k, mv in os.environ.items():
            s = s.replace(f"${{{k}}}", mv)
        return s

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "application/vnd.github+json",
            "authorization": f"Bearer {self._token}",
            "x-github-api-version": "2022-11-28",
            "user-agent": "issue-keeper/0.2",
        }

    def _api(self, path: str, *, method: str = "GET", body: dict | None = None,
             params: dict | None = None) -> Any:
        url = GITHUB_API + path
        if params:
            qs = urllib.parse.urlencode([(k, v) for k, v in params.items() if v is not None])
            if qs:
                url += "?" + qs
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method, headers=self._headers()
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"GitHub API {method} {path} 失败: {e.code} {err}") from e
        if not raw:
            return None
        return json.loads(raw)

    def _list_paged(self, path: str, kind: str, repo: str,
                    labels: list[str] | None) -> list[Resource]:
        # GitHub REST: per_page 最大 100，page 从 1 开始
        results: list[Resource] = []
        page = 1
        while True:
            params: dict[str, Any] = {"state": "open", "per_page": 100, "page": page}
            if labels:
                params["labels"] = ",".join(labels)
            rows = self._api(path, params=params) or []
            if not rows:
                break
            results.extend(_row_to_resource(r, kind, repo) for r in rows)
            if len(rows) < 100:
                break  # 最后一页
            page += 1
            if page > 20:  # 兜底：最多 2000 条
                break
        return results

    def list_open(
        self, repo: str, kinds: list[str], labels: list[str] | None = None
    ) -> list[Resource]:
        out: list[Resource] = []
        for kind in kinds:
            if kind == "issue":
                path = f"/repos/{repo}/issues"
            elif kind == "pr":
                path = f"/repos/{repo}/pulls"
            else:
                continue
            out.extend(self._list_paged(path, kind, repo, labels))
        return out

    def list_comments(self, repo: str, resource: Resource) -> list[Comment]:
        # issue 和 PR 的评论都用 issues/{n}/comments endpoint（PR 的 review comments 是另一套）
        path = f"/repos/{repo}/issues/{resource.number}/comments"
        rows = self._api(path, params={"per_page": 100}) or []
        comments: list[Comment] = []
        for c in rows:
            comments.append(
                Comment(
                    id=str(c.get("id", "")),
                    url=c.get("html_url", "") or "",
                    author=_parse_author(c.get("user")),
                    body=c.get("body", "") or "",
                    created_at=c.get("created_at", ""),
                )
            )
        comments.sort(key=lambda c: c.id)
        return comments

    def post_comment(self, repo: str, resource: Resource, body: str) -> None:
        path = f"/repos/{repo}/issues/{resource.number}/comments"
        self._api(path, method="POST", body={"body": body})

    def self_identity(self) -> str:
        if self._identity is None:
            try:
                data = self._api("/user") or {}
                self._identity = str(data.get("login") or "")
            except RuntimeError:
                self._identity = ""
        return self._identity

    def web_url(self, repo: str, resource: Resource) -> str:
        return _gh_url(repo, resource)


# ─────────────────────────────────────────────────────────────────────
# 模块级便捷函数（向后兼容旧调用方）
# ─────────────────────────────────────────────────────────────────────

_default = GitHubSource()


def list_open_issues(repo: str, labels: list[str] | None = None) -> list[Resource]:
    return _default.list_open(repo, ["issue"], labels)


def list_open_prs(repo: str, labels: list[str] | None = None) -> list[Resource]:
    return _default.list_open(repo, ["pr"], labels)


def list_open(repo: str, kind: str, labels: list[str] | None = None) -> list[Resource]:
    return _default.list_open(repo, [kind], labels)


def list_comments(repo: str, kind: str, number: int) -> list[Comment]:
    """旧签名。新代码请用 source.list_comments(repo, resource)。"""
    stub = Resource(kind=kind, number=number, title="", body="", state="",
                    labels=[], author="", created_at="", updated_at="", source_ref="")
    return _default.list_comments(repo, stub)


def post_comment(repo: str, kind: str, number: int, body: str) -> None:
    """旧签名。新代码请用 source.post_comment(repo, resource, body)。"""
    stub = Resource(kind=kind, number=number, title="", body="", state="",
                    labels=[], author="", created_at="", updated_at="", source_ref="")
    _default.post_comment(repo, stub, body)


def post_comment_for(repo: str, kind: str, number: int, body: str) -> None:
    post_comment(repo, kind, number, body)


def whoami() -> str:
    return _default.self_identity()
