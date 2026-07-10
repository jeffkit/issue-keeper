"""已处理 issue / 评论 / agent 会话的状态持久化。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class IssueState:
    processed: bool = False  # issue 本体（首次创建）是否已处理
    session_id: str | None = None  # agent 返回的会话 uuid，用于续接
    processed_comment_ids: set[str] = field(default_factory=set)


@dataclass
class RepoState:
    issues: dict[int, IssueState] = field(default_factory=dict)

    def issue(self, number: int) -> IssueState:
        if number not in self.issues:
            self.issues[number] = IssueState()
        return self.issues[number]


@dataclass
class State:
    repos: dict[str, RepoState] = field(default_factory=dict)

    def repo(self, repo_slug: str) -> RepoState:
        if repo_slug not in self.repos:
            self.repos[repo_slug] = RepoState()
        return self.repos[repo_slug]


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) or {}
    state = State()
    for repo_slug, rdata in (raw.get("repos") or {}).items():
        rs = state.repo(repo_slug)
        for num_str, idata in (rdata.get("issues") or {}).items():
            num = int(num_str)
            is_ = rs.issue(num)
            is_.processed = bool(idata.get("processed", False))
            is_.session_id = idata.get("session_id")
            is_.processed_comment_ids = set(str(x) for x in (idata.get("processed_comment_ids") or []))
    return state


def save_state(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {"repos": {}}
    for repo_slug, rs in state.repos.items():
        raw["repos"][repo_slug] = {
            "issues": {
                str(num): {
                    "processed": is_.processed,
                    "session_id": is_.session_id,
                    "processed_comment_ids": sorted(is_.processed_comment_ids),
                }
                for num, is_ in rs.issues.items()
            }
        }
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
