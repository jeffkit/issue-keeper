"""自建 issue 系统的 IssueSource 实现。

完全脱离 GitHub，用 SQLite 存储 issue/PR 与评论。给 agent 之间互相沟通用：
- 每个 repo 配置项对应一个"项目"（repo 字段是任意项目名）
- agent 通过 IssueSource 协议读写自己负责的项目
- 跨项目沟通：A 的 keeper 配置里加 B 项目作为另一个 repo binding，A 以自己的
  agent_label 身份在 B 那边提 issue / 留评论
- 提 issue / 以外部身份留言：通过 issue_keeper.internal_cli 子命令

存储：单个 SQLite 文件，WAL 模式（支持多进程并发读写）。
默认路径：~/.issue-keeper/internal.db，可被 binding.internal_db 覆盖。

身份模型：
- self_identity() 返回 binding.agent_label（即配置里那个 agent 身份标签）
- agent 通过 source.post_comment 发评论时，author 自动记为 agent_label
- 通过 CLI 提 issue / 评论时，author 由 --author 参数指定，--actor-type 指定角色

状态机（6 个状态）：
- inbox  收件箱，原始未规划
- todo   已规划待处理
- doing  处理中
- review 待 review
- done   已完成
- closed 已关闭/归档

流转规则（先宽松，任意状态都能转到任意状态，只记 history）：
- 新建 issue（人提）→ inbox
- 新建 issue（agent 提）→ todo
- keeper 开始调 agent → doing
- agent 回复完 → review
- review 通过 → done
- close → closed

review 规则：
- agent 提的 issue → 该 agent 自己 review（下轮扫到 review 自动通过）
- 人提的 issue → 默认 review_agent 先 review；需要人二次 review 时人接手

注意：和 GitHub 不同，internal source 的 issue 编号是数据库自增的，
issue#5 和 PR#5 不会并存（同一表），因此 resource_key 直接用 number。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import Comment, IssueSource, Resource

DEFAULT_DB = Path.home() / ".issue-keeper" / "internal.db"

# 状态枚举（顺序即看板列顺序）
STATUSES = ("inbox", "todo", "doing", "review", "done", "closed")
DEFAULT_STATUS_HUMAN = "inbox"   # 人提的 issue 默认进 inbox
DEFAULT_STATUS_AGENT = "todo"    # agent 提的 issue 默认进 todo
ACTIVE_STATUSES = ("inbox", "todo", "doing", "review")  # 这些状态算"开放"，done/closed 不算

ACTOR_HUMAN = "human"
ACTOR_AGENT = "agent"


_SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT    NOT NULL,                 -- 对应 binding.repo，任意项目名
    kind        TEXT    NOT NULL DEFAULT 'issue', -- 'issue' | 'pr'
    number      INTEGER NOT NULL,                 -- 项目内递增编号
    title       TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL DEFAULT '',
    state       TEXT    NOT NULL DEFAULT 'open',  -- 'open' | 'closed'（保留兼容）
    status      TEXT    NOT NULL DEFAULT 'inbox', -- 看板状态：inbox/todo/doing/review/done/closed
    author      TEXT    NOT NULL DEFAULT '',
    actor_type  TEXT    NOT NULL DEFAULT 'human', -- 'human' | 'agent'
    assignee    TEXT    NOT NULL DEFAULT '',      -- 当前负责人
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    UNIQUE(project, kind, number)
);

CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    issue_number INTEGER NOT NULL,
    author      TEXT    NOT NULL DEFAULT '',
    actor_type  TEXT    NOT NULL DEFAULT 'human',
    body        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS status_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    issue_number INTEGER NOT NULL,
    from_status TEXT,
    to_status   TEXT    NOT NULL,
    actor       TEXT    NOT NULL,
    actor_type  TEXT    NOT NULL DEFAULT 'human',
    comment     TEXT,
    created_at  TEXT    NOT NULL
);
"""

_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_issues_project_state
    ON issues(project, state);
CREATE INDEX IF NOT EXISTS idx_issues_project_status
    ON issues(project, status);
CREATE INDEX IF NOT EXISTS idx_comments_issue
    ON comments(project, kind, issue_number, id);
CREATE INDEX IF NOT EXISTS idx_status_history_issue
    ON status_history(project, kind, issue_number, id);
"""


def _now_iso() -> str:
    """生成 ISO 时间戳。sqlite3 不支持直接传 datetime，用字符串。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InternalSource(IssueSource):
    """基于 SQLite 的自建 issue 系统适配器。"""

    def __init__(self, binding) -> None:
        self._binding = binding
        db_path = getattr(binding, "internal_db", None) or str(DEFAULT_DB)
        db_path = self._expand_env(str(db_path))
        # 支持路径展开
        self._db_path = str(Path(db_path).expanduser())
        self._identity = binding.agent_label or "issue-keeper"
        # 每个实例一个锁，防止并发写冲突（虽然 SQLite 自己也有锁）
        self._lock = threading.Lock()
        self._init_schema()

    @staticmethod
    def _expand_env(value: str) -> str:
        s = str(value)
        for k, mv in os.environ.items():
            s = s.replace(f"${{{k}}}", mv)
        return s

    def _conn(self) -> sqlite3.Connection:
        """每次操作开一个新连接。SQLite 连接轻量，且避免多线程共享问题。"""
        conn = sqlite3.connect(self._db_path, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        # WAL 模式：多进程可并发读，写串行
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._conn()
            try:
                # 1) 先建表（旧表已存在会跳过）
                conn.executescript(_SCHEMA_TABLES)
                # 2) 迁移旧表（加新字段）
                self._migrate(conn)
                # 3) 建索引（迁移后字段都齐了）
                conn.executescript(_SCHEMA_INDEXES)
            finally:
                conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """旧库迁移：补齐新加的字段。SQLite 的 ALTER TABLE ADD COLUMN 幂等性靠 PRAGMA table_info 检查。"""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(issues)").fetchall()}
        if "status" not in cols:
            conn.execute("ALTER TABLE issues ADD COLUMN status TEXT NOT NULL DEFAULT 'inbox'")
        if "actor_type" not in cols:
            conn.execute("ALTER TABLE issues ADD COLUMN actor_type TEXT NOT NULL DEFAULT 'human'")
        if "assignee" not in cols:
            conn.execute("ALTER TABLE issues ADD COLUMN assignee TEXT NOT NULL DEFAULT ''")
        # 旧 issue 的 state='open' 但 status 是 inbox——保持不变，让 keeper 视为开放
        # closed 的旧 issue 同步把 status 设为 closed
        conn.execute("UPDATE issues SET status = 'closed' WHERE state = 'closed' AND status = 'inbox'")

        ccols = {row["name"] for row in conn.execute("PRAGMA table_info(comments)").fetchall()}
        if "actor_type" not in ccols:
            conn.execute("ALTER TABLE comments ADD COLUMN actor_type TEXT NOT NULL DEFAULT 'human'")

    def _row_to_resource(self, row: sqlite3.Row) -> Resource:
        return Resource(
            kind=row["kind"],
            number=row["number"],
            title=row["title"],
            body=row["body"],
            state=row["state"],
            labels=[],  # internal 暂不支持 label 过滤
            author=row["author"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            source_ref=f"{row['project']}#{row['kind']}-{row['number']}",
            status=row["status"],
            actor_type=row["actor_type"],
            assignee=row["assignee"],
        )

    def list_open(
        self, repo: str, kinds: list[str], labels: list[str] | None = None
    ) -> list[Resource]:
        """列出某项目的开放资源（status 在 ACTIVE_STATUSES 里）。

        keeper 主循环扫"开放"的 issue——done/closed 不再处理。
        labels 在 internal 里暂不支持过滤。
        """
        if not kinds:
            return []
        kind_ph = ",".join("?" * len(kinds))
        status_ph = ",".join("?" * len(ACTIVE_STATUSES))
        sql = (
            f"SELECT * FROM issues "
            f"WHERE project = ? AND kind IN ({kind_ph}) AND status IN ({status_ph}) "
            f"ORDER BY kind, number"
        )
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(sql, [repo, *kinds, *ACTIVE_STATUSES]).fetchall()
            finally:
                conn.close()
        return [self._row_to_resource(r) for r in rows]

    def list_all(self, repo: str, kinds: list[str] | None = None) -> list[Resource]:
        """列出某项目的全部 issue（任意状态），给看板视图用。"""
        kinds = kinds or ["issue", "pr"]
        kind_ph = ",".join("?" * len(kinds))
        sql = (
            f"SELECT * FROM issues "
            f"WHERE project = ? AND kind IN ({kind_ph}) "
            f"ORDER BY kind, number"
        )
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(sql, [repo, *kinds]).fetchall()
            finally:
                conn.close()
        return [self._row_to_resource(r) for r in rows]

    def list_comments(self, repo: str, resource: Resource) -> list[Comment]:
        sql = (
            "SELECT * FROM comments "
            "WHERE project = ? AND kind = ? AND issue_number = ? "
            "ORDER BY id"
        )
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    sql, [repo, resource.kind, resource.number]
                ).fetchall()
            finally:
                conn.close()
        return [
            Comment(
                id=str(r["id"]),
                url="",  # internal 暂无 web UI
                author=r["author"],
                body=r["body"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def post_comment(self, repo: str, resource: Resource, body: str) -> None:
        """以当前 agent 身份（agent_label）发评论。同时更新 issue 的 updated_at。"""
        now = _now_iso()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO comments (project, kind, issue_number, author, actor_type, body, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [repo, resource.kind, resource.number, self._identity, ACTOR_AGENT, body, now],
                )
                conn.execute(
                    "UPDATE issues SET updated_at = ? WHERE project = ? AND kind = ? AND number = ?",
                    [now, repo, resource.kind, resource.number],
                )
            finally:
                conn.close()

    def self_identity(self) -> str:
        return self._identity

    def web_url(self, repo: str, resource: Resource) -> str:
        # 暂无 web dashboard，返回空串。未来挂 HTTP server 时填本地 URL。
        return ""

    # ---- 提供给 CLI 用的额外方法（不在 IssueSource 协议内） -------------

    def create_issue(
        self, repo: str, kind: str, title: str, body: str, author: str,
        *, actor_type: str = ACTOR_HUMAN,
    ) -> Resource:
        """提一个新 issue/PR。编号在 (project, kind) 内递增。

        actor_type=human → 默认 status=inbox（等人规划）
        actor_type=agent → 默认 status=todo（直接待处理）
        """
        default_status = DEFAULT_STATUS_AGENT if actor_type == ACTOR_AGENT else DEFAULT_STATUS_HUMAN
        now = _now_iso()
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(number), 0) + 1 AS next_num "
                    "FROM issues WHERE project = ? AND kind = ?",
                    [repo, kind],
                ).fetchone()
                next_num = int(row["next_num"])
                conn.execute(
                    "INSERT INTO issues "
                    "(project, kind, number, title, body, state, status, author, actor_type, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                    [repo, kind, next_num, title, body, default_status, author, actor_type, now, now],
                )
                row = conn.execute(
                    "SELECT * FROM issues WHERE project = ? AND kind = ? AND number = ?",
                    [repo, kind, next_num],
                ).fetchone()
                # 记初始 status_history
                conn.execute(
                    "INSERT INTO status_history (project, kind, issue_number, from_status, to_status, actor, actor_type, comment, created_at) "
                    "VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?)",
                    [repo, kind, next_num, default_status, author, actor_type, now],
                )
            finally:
                conn.close()
        return self._row_to_resource(row)

    def add_comment(
        self, repo: str, resource: Resource, body: str, author: str,
        *, actor_type: str = ACTOR_HUMAN,
    ) -> Comment:
        """以指定 author 身份发评论（CLI 用，可模拟外部人/agent 留言）。"""
        now = _now_iso()
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO comments (project, kind, issue_number, author, actor_type, body, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [repo, resource.kind, resource.number, author, actor_type, body, now],
                )
                cid = cur.lastrowid
                conn.execute(
                    "UPDATE issues SET updated_at = ? WHERE project = ? AND kind = ? AND number = ?",
                    [now, repo, resource.kind, resource.number],
                )
                row = conn.execute(
                    "SELECT * FROM comments WHERE id = ?", [cid]
                ).fetchone()
            finally:
                conn.close()
        return Comment(
            id=str(row["id"]),
            url="",
            author=row["author"],
            body=row["body"],
            created_at=row["created_at"],
        )

    def get_issue(self, repo: str, kind: str, number: int) -> Resource | None:
        """按编号查单个 issue（任意状态）。"""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM issues WHERE project = ? AND kind = ? AND number = ?",
                    [repo, kind, number],
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_resource(row) if row else None

    def move_status(
        self, repo: str, resource: Resource, to_status: str,
        *, actor: str = "", actor_type: str = ACTOR_HUMAN, comment: str = "",
    ) -> tuple[bool, str]:
        """改 issue 状态。返回 (是否真的改了, 前一个状态)。

        - to_status 必须是 STATUSES 之一
        - 记 status_history
        - 同时更新 issue.assignee（如果是 review 状态且 actor 是 agent，把 assignee 设为该 actor）
        """
        if to_status not in STATUSES:
            raise ValueError(f"非法 status: {to_status}，合法值: {STATUSES}")

        now = _now_iso()
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT status FROM issues WHERE project = ? AND kind = ? AND number = ?",
                    [repo, resource.kind, resource.number],
                ).fetchone()
                if row is None:
                    return False, ""
                from_status = row["status"]
                if from_status == to_status:
                    return False, from_status

                # review 状态时，如果是 agent 接手，assignee 设为该 agent
                assignee_clause = ""
                assignee_val: list = []
                if to_status == "review" and actor and actor_type == ACTOR_AGENT:
                    assignee_clause = ", assignee = ?"
                    assignee_val = [actor]
                elif to_status in ("done", "closed"):
                    assignee_clause = ", assignee = ''"
                elif to_status == "doing" and actor:
                    assignee_clause = ", assignee = ?"
                    assignee_val = [actor]

                conn.execute(
                    f"UPDATE issues SET status = ?, updated_at = ?{assignee_clause} "
                    f"WHERE project = ? AND kind = ? AND number = ?",
                    [to_status, now, *assignee_val, repo, resource.kind, resource.number],
                )
                conn.execute(
                    "INSERT INTO status_history "
                    "(project, kind, issue_number, from_status, to_status, actor, actor_type, comment, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [repo, resource.kind, resource.number, from_status, to_status,
                     actor or "unknown", actor_type, comment or None, now],
                )
            finally:
                conn.close()
        return True, from_status

    def list_status_history(
        self, repo: str, resource: Resource
    ) -> list[dict[str, Any]]:
        """某 issue 的状态变更历史。"""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM status_history "
                    "WHERE project = ? AND kind = ? AND issue_number = ? "
                    "ORDER BY id",
                    [repo, resource.kind, resource.number],
                ).fetchall()
            finally:
                conn.close()
        return [dict(r) for r in rows]

    def close_issue(self, repo: str, resource: Resource,
                    *, actor: str = "", actor_type: str = ACTOR_HUMAN) -> bool:
        """关闭 issue/PR（status=closed, state=closed）。返回是否真的改了。"""
        ok, _ = self.move_status(
            repo, resource, "closed", actor=actor, actor_type=actor_type,
        )
        if ok:
            now = _now_iso()
            with self._lock:
                conn = self._conn()
                try:
                    conn.execute(
                        "UPDATE issues SET state = 'closed', updated_at = ? "
                        "WHERE project = ? AND kind = ? AND number = ?",
                        [now, repo, resource.kind, resource.number],
                    )
                finally:
                    conn.close()
        return ok
