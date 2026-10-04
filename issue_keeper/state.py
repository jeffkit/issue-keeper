"""已处理 issue / PR / 评论 / agent 会话的状态持久化。

资源 key 规范：
- issue: 纯数字字符串（如 "42"），与历史 state.json 兼容
- PR:    "pr:42"，与 issue 隔离会话与处理进度
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class ItemState:
    processed: bool = False  # 资源本体（首次创建）是否已处理
    session_id: str | None = None  # agent 返回的会话 uuid，用于续接
    processed_comment_ids: set[str] = field(default_factory=set)
    blocked: bool = False  # 安全过滤命中，后续不再自动处理
    # 依赖唤醒监视（仅 issue）：pipeline 终态 blocked 时记录正文引用的依赖编号。
    # 每轮检查——依赖全部闭合（关闭/修复已进 origin/main）→ 清 processed 唤醒重跑。
    # 空列表 = 未在监视。
    wakeup_deps: list[int] = field(default_factory=list)
    # 管线 run 在途标记（2026-09-29 派发解耦）：dispatch 时写 epoch 秒，reaper
    # 收尾清回 None。非 None 期间该资源整体跳过（reaper 拥有它），避免重复派发。
    in_flight_since: float | None = None
    # 评论层异步任务（2026-10-01 评论层后台化）：comment_id(str) →
    # {"pid": int|None, "started_at": float, "attempts": int}。派发时写入、
    # 收尸后清除；daemon 重启后按 pid 存活接管（防重复调起）。
    comment_tasks: dict = field(default_factory=dict)


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
            it.wakeup_deps = [int(x) for x in (idata.get("wakeup_deps") or [])]
            it.in_flight_since = (
                float(idata["in_flight_since"]) if idata.get("in_flight_since") else None)
            it.comment_tasks = dict(idata.get("comment_tasks") or {})
    state.patrol = dict(raw.get("patrol") or {})
    state.patrol_cycle = int(raw.get("patrol_cycle") or 0)
    return state


@contextmanager
def _state_lock(path: Path):
    """state 写锁：与写回竞态（2026-09-29 实证）——长周期结束时把内存旧状态整份写回，
    会覆盖轮中途的 `reopen`。所有写路径都先拿这把锁。"""
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with open(lock, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def save_state(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock(path):
        _save_state_unlocked(path, state)


def _save_state_unlocked(path: Path, state: State) -> None:
    raw = _dump_state_dict(state)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(raw, indent=2, ensure_ascii=False))
        os.replace(tmp, path)  # 原子替换：进程死在写中间也不会留半个 state.json
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _dump_state_dict(state: State) -> dict[str, Any]:
    raw: dict[str, Any] = {"repos": {}}
    for repo_slug, rs in state.repos.items():
        raw["repos"][repo_slug] = {
            "items": {
                key: {
                    "processed": it.processed,
                    "session_id": it.session_id,
                    "processed_comment_ids": sorted(it.processed_comment_ids),
                    "blocked": it.blocked,
                    "wakeup_deps": it.wakeup_deps,
                    "in_flight_since": it.in_flight_since,
                    "comment_tasks": it.comment_tasks,
                }
                for key, it in rs.items.items()
            }
        }
    raw["patrol"] = dict(state.patrol)
    raw["patrol_cycle"] = state.patrol_cycle
    return raw


def save_state_item(
    path: Path, repo_slug: str, key: str, mutate: Callable[[ItemState], None],
) -> None:
    """单条合并写：持锁重读盘上最新状态 → 对这条 item 施加 mutate → 原子写回。

    解 2026-09-29 的竞态：daemon 一个长周期结束时把**整份**内存状态写回，
    会覆盖周期中途 `reopen` 的改动。逐条合并后粒度从「整份文件」缩到「单条 item」，
    且只有调用方显式改的字段会变（不用调用方的旧对象整条替换）。
    """
    with _state_lock(path):
        state = load_state_unlocked(path)
        mutate(state.repo(repo_slug).item(str(key)))
        _save_state_unlocked(path, state)


_ITEM_FIELDS = ("processed", "session_id", "processed_comment_ids", "blocked",
                "wakeup_deps", "in_flight_since", "comment_tasks")


def save_state_merged(path: Path, state: State, base: State) -> None:
    """轮尾合并写：持锁重读盘上状态，只把 `state` 相对 `base` 变过的字段写回。

    daemon 一轮很长（轮首 load → 轮尾写），期间 CLI 的 `reopen` 已写盘的字段必须保留：
    逐字段与轮首快照 diff，变过的（本轮真改过）写内存值，没变的一律保留盘上值。
    """
    with _state_lock(path):
        disk = load_state_unlocked(path)
        _merge_state(disk, state, base)
        _save_state_unlocked(path, disk)


def _merge_state(disk: State, mem: State, base: State) -> None:
    for slug, rs in mem.repos.items():
        drs = disk.repo(slug)
        brs = base.repos.get(slug)
        for key, item in rs.items.items():
            base_item = brs.items.get(key) if brs else None
            target = drs.item(key)
            for name in _ITEM_FIELDS:
                value = getattr(item, name)
                if base_item is None or getattr(base_item, name) != value:
                    setattr(target, name, value)
    for key, value in mem.patrol.items():
        if base.patrol.get(key) != value:
            disk.patrol[key] = value
    if mem.patrol_cycle != base.patrol_cycle:
        disk.patrol_cycle = mem.patrol_cycle


def load_state_unlocked(path: Path) -> State:
    return load_state(path)
