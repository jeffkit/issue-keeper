"""向后兼容模块：所有符号从 sources.github 重导出。

新代码请直接用 issue_keeper.sources 里的协议/类型，或 issue_keeper.sources.github
里的 GitHubSource。这个模块只是让 keeper.gh.xxx 这种旧调用继续工作。
"""

from __future__ import annotations

from .sources import Comment, IssueSource, Resource
from .sources.github import (
    GitHubSource,
    list_comments,
    list_open,
    list_open_issues,
    list_open_prs,
    post_comment,
    post_comment_for,
    whoami,
)

__all__ = [
    "Comment",
    "IssueSource",
    "Resource",
    "GitHubSource",
    "list_open_issues",
    "list_open_prs",
    "list_open",
    "list_comments",
    "post_comment",
    "post_comment_for",
    "whoami",
]
