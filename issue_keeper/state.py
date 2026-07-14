"""已处理 issue / PR / 评论 / agent 会话的状态持久化。

资源 key 规范：
- issue: 纯数字字符串（如 "42"），与历史 state.json 兼容
- PR:    "pr:42"，与 issue 隔离会话与处理进度
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ItemState:
    processed: bool = False  # 资源本体（首次创建）是否已处理
    session_id: str | None = None  # agent 返回的会话 uuid，用于续接
    processed_comment_ids: set[str] = field(default_factory=set)
    blocked: bool = False  # 安全过滤命中，后续不再自动处理


@dataclass
class RepoState:
    items: dict[str, ItemState] = field(default_factory=dict)

    def item(self, key: str) -> ItemState:
        if key not in self.items:
            self.items[key] = ItemState()
        return self.items[key]


@dataclass
class State:
    repos: dict[str, RepoState] = field(default_factory=dict)
    # keeper 巡检：key 形如 "<repo_slug>:<kind>:<number>" → {"updated_at": ..., "session_id": ...}
    # 记录每条 issue 上次巡检时的 updated_at 快照，没新活动就不重复巡检/HitL（防刷屏）。
    patrol: dict[str, dict] = field(default_factory=dict)
    patrol_cycle: int = 0  # daemon 轮次计数，用于按 interval_cycles 节流巡检

    def repo(self, repo_slug: str) -> RepoState:
        if repo_slug not in self.repos:
            self.repos[repo_slug] = RepoState()
        return self.repos[repo_slug]

    def patrol_key(self, repo_slug: str, kind: str, number: int) -> str:
        return f"{repo_slug}:{kind}:{number}"

    def patrol_snapshot(self, key: str) -> str:
        """上次巡检时记下的 updated_at；没有返回空串。"""
        return (self.patrol.get(key) or {}).get("updated_at") or ""

    def mark_patrolled(self, key: str, updated_at: str, session_id: str | None) -> None:
        d = {"updated_at": updated_at}
        if session_id:
            d["session_id"] = session_id
        self.patrol[key] = d


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) or {}
    state = State()
    for repo_slug, rdata in (raw.get("repos") or {}).items():
        rs = state.repo(repo_slug)
        # 兼容旧字段名 issues
        items_src = (rdata.get("items") or rdata.get("issues") or {})
        for key, idata in items_src.items():
            it = rs.item(str(key))
            it.processed = bool(idata.get("processed", False))
            it.session_id = idata.get("session_id")
            it.processed_comment_ids = set(str(x) for x in (idata.get("processed_comment_ids") or []))
            it.blocked = bool(idata.get("blocked", False))
    state.patrol = dict(raw.get("patrol") or {})
    state.patrol_cycle = int(raw.get("patrol_cycle") or 0)
    return state


def save_state(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {"repos": {}}
    for repo_slug, rs in state.repos.items():
        raw["repos"][repo_slug] = {
            "items": {
                key: {
                    "processed": it.processed,
                    "session_id": it.session_id,
                    "processed_comment_ids": sorted(it.processed_comment_ids),
                    "blocked": it.blocked,
                }
                for key, it in rs.items.items()
            }
        }
    raw["patrol"] = state.patrol
    raw["patrol_cycle"] = state.patrol_cycle
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
