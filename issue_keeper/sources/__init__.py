"""可插拔的 issue/PR 来源层。

issue-keeper 的核心循环对来源不敏感：只要某个来源实现了 IssueSource 协议，
就可以被 keeper 当作"GitHub issue"用。这样我们能接入各种上游：
- GitHub CLI（用你账号）
- GitHub App（独立 bot 身份）
- Discord / Slack / 自建 HTTP 接口（未来）

当前实现：GitHubSource（issue_keeper/sources/github.py）。

协议方法说明：
- list_open(kinds, labels): 列出 open issue/PR，可按 label 过滤。kinds 是 ["issue"]
  或 ["issue", "pr"] 的子集；labels 为 None 表示不过滤。某些来源可能不支持 label
  过滤或 PR（比如未来的 Discord 适配器），允许在能力上做减法。
- list_comments(resource): 读资源下的普通评论（按时间升序）。
- post_comment(resource, body): 在资源下发表评论。
- self_identity(): 返回当前身份标识（如 GitHub 用户名），用于"忽略自己发的评论"。

所有方法都可能抛异常；keeper 会捕获并跳过该来源/该资源，不影响其他资源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class Comment:
    """一条评论的来源无关视图。"""

    id: str  # 来源内稳定唯一 ID（用作去重 key）
    url: str  # 评论可访问 URL（无则空串）
    author: str  # 评论作者在该来源里的标识
    body: str
    created_at: str


@dataclass
class Resource:
    """一个 issue/PR 的来源无关视图。"""

    kind: str  # "issue" | "pr"（来源不支持 PR 时只会出现 "issue"）
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    author: str
    created_at: str
    updated_at: str
    # 来源内部用来定位资源的 key（如 "owner/repo#issue-42"），用于日志/调试
    source_ref: str = ""
    # 以下字段对 GitHub source 可为空/默认；internal source 会填实际值
    status: str = ""  # 看板状态（internal: inbox/todo/doing/review/done/closed）
    actor_type: str = ""  # author 的角色：human / agent
    assignee: str = ""  # 当前负责人

    @property
    def resource_key(self) -> str:
        """state.py 里使用的 key。issue 为纯数字，PR 为 'pr:N'。"""
        return str(self.number) if self.kind == "issue" else f"pr:{self.number}"

    @property
    def noun(self) -> str:
        return "issue" if self.kind == "issue" else "PR"


@runtime_checkable
class IssueSource(Protocol):
    """一个 issue/PR 上游适配器需要实现的协议。"""

    def list_open(
        self, repo: str, kinds: list[str], labels: list[str] | None = None
    ) -> list[Resource]:
        """列出某仓库的 open 资源。kinds ∈ {"issue","pr"} 子集。"""
        ...

    def list_comments(self, repo: str, resource: Resource) -> list[Comment]:
        ...

    def post_comment(self, repo: str, resource: Resource, body: str) -> None:
        ...

    def self_identity(self) -> str:
        """当前身份标识（如 GitHub login），用于忽略自己发的评论。空串表示未知。"""
        ...

    def web_url(self, repo: str, resource: Resource) -> str:
        """资源在来源里的可访问 URL。无 URL 的来源返回空串。"""
        ...


# source 名字 → 工厂函数。新来源在这里注册一行即可被 config 用上。
def make_source(name: str, binding=None) -> IssueSource:
    """根据 source 名字实例化一个 IssueSource。

    目前支持：
        github_cli   —— 用 gh CLI 访问 GitHub（默认，用当前登录账号身份）
        github_token —— 用 PAT 直接调 GitHub REST API，可指定任意账号身份
        internal     —— 自建 issue 系统（SQLite 存储，完全脱离 GitHub）

    未来可加：github_app（独立 bot 身份）、discord、http、...

    binding 是可选的 RepoBinding，某些 source 需要从中读取凭据/参数
    （如 github_token 需要 token / agent_label，internal 需要 internal_db / agent_label）。
    """
    n = (name or "").strip().lower()
    if n in ("", "github_cli", "github", "gh"):
        from .github import GitHubSource
        return GitHubSource()
    if n == "github_token":
        from .github import GitHubTokenSource
        if binding is None:
            raise ValueError("github_token source 需要 binding 配置（token / agent_label）")
        return GitHubTokenSource(binding=binding)
    if n == "internal":
        from .internal import InternalSource
        if binding is None:
            raise ValueError("internal source 需要 binding 配置（agent_label / internal_db）")
        return InternalSource(binding=binding)
    raise ValueError(
        f"未知 source '{name}'。当前支持: github_cli, github_token, internal。"
        f"（未来: github_app / discord / http ...）"
    )
