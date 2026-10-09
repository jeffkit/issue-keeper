"""duty 内核 —— 值守协同的结构化状态存储（文件为准，零运行时依赖）。

取代 ~/.issue-keeper/pipeline/controller/*.md 的机器可读内核。
定位与边界（详见 docs/duty/DUTY-PROTOCOL.md）：
- **事实来源是文件**（~/.issue-keeper/duty/），Redis 只做通知、markdown 只做渲染；
- 复用 state.py 的并发语义思想：flock 互斥 + os.replace 原子写 + 轮尾合并；
- Schema 契约：docs/duty/schema/*.json（validate.py 做契约测试）。运行时只做
  **轻量结构校验**（无 jsonschema 依赖，worker 子进程可直接 import）——
  拦截明显损坏的写入，完整校验留给契约测试与 CI。

用法（flow 的 CODE 节点 / CLI 均可）：
    from issue_keeper.duty import DutyStore
    store = DutyStore()                      # 默认 ~/.issue-keeper/duty
    store.append_round("issue-accept", {...})  # 轮次状态滚动窗口（保留 20 轮）
    store.write_handoff("issue-accept", {...}) # 滚动交接（保留 20 份）
    store.load_state("issue-accept")           # 读最近轮次
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEFAULT_BASE = Path("~/.issue-keeper/duty").expanduser()

#: 轮次状态/交接的滚动保留窗口（协议 §4.2：不做全历史，rounds.log 是反面样本）
KEEP_ROUNDS = 20
KEEP_HANDOFFS = 20

_ROLES = {"A", "B", "controller", "sandbox"}
_FLOW_ROLES = set(_ROLES) | {
    # flow 形态的值守角色：以 flow 名为 state 文件名（一个 flow 一份轮次序列）
    "issue-accept", "sandbox", "patrol",
}
_ROUND_REQUIRED = ("schema_version", "role", "round", "started_at", "status")
_ROUND_STATUS = {"running", "ok", "attention", "failed", "aborted"}
_HANDOFF_REQUIRED = ("schema_version", "role", "generation", "round", "written_at")


class DutyValidationError(ValueError):
    """结构校验失败——写入被拒绝（fail-closed）。"""


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise DutyValidationError(msg)


def _validate_round(doc: dict[str, Any]) -> None:
    for k in _ROUND_REQUIRED:
        _check(k in doc, f"round 缺必填字段 {k}")
    _check(doc["schema_version"] == "duty/state@0", "schema_version 必须为 duty/state@0")
    _check(doc["role"] in _FLOW_ROLES, f"role 非法: {doc['role']!r}")
    _check(doc["status"] in _ROUND_STATUS, f"status 非法: {doc['status']!r}")
    _check(isinstance(doc.get("round"), int) and doc["round"] >= 1, "round 必须为正整数")
    findings = doc.get("findings") or []
    _check(isinstance(findings, list), "findings 必须为数组")
    for f in findings:
        _check((f.get("severity") or "info") in {"info", "warn", "critical"},
               f"finding.severity 非法: {f.get('severity')!r}")


def _validate_handoff(doc: dict[str, Any]) -> None:
    for k in _HANDOFF_REQUIRED:
        _check(k in doc, f"handoff 缺必填字段 {k}")
    _check(doc["schema_version"] == "duty/handoff@0", "schema_version 必须为 duty/handoff@0")
    _check(doc["role"] in _FLOW_ROLES, f"role 非法: {doc['role']!r}")


@contextmanager
def _file_lock(path: Path):
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, doc: dict[str, Any]) -> None:
    """原子写：临时文件 + os.replace——进程死在写中间不留半个文件（同 state.py）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class DutyStore:
    """duty 内核存储：轮次状态（滚动窗口）+ 滚动交接 + roster。"""

    def __init__(self, base: Path | str | None = None) -> None:
        self.base = Path(base).expanduser() if base else DEFAULT_BASE
        self.handoff_dir = self.base / "handoffs"

    # ---- 轮次状态：一个 flow 一份文件，内含滚动窗口 ----

    def _state_path(self, role: str) -> Path:
        return self.base / f"state-{role}.json"

    def load_state(self, role: str) -> dict[str, Any]:
        """返回 {"rounds": [最旧→最新], ...}；无历史返回空骨架。"""
        doc = _read_json(self._state_path(role), {})
        if not doc:
            return {"schema_version": "duty/state@0", "role": role, "rounds": []}
        return doc

    def append_round(self, role: str, round_doc: dict[str, Any]) -> None:
        """追加一轮并裁剪窗口。持锁重读盘上最新 → 追加 → 原子写回（防覆盖并发写）。"""
        _validate_round(round_doc)
        with _file_lock(self._state_path(role)):
            doc = self.load_state(role)
            rounds = doc.setdefault("rounds", [])
            rounds.append(round_doc)
            doc["rounds"] = rounds[-KEEP_ROUNDS:]
            doc["role"] = role
            doc["updated_at"] = round_doc.get("finished_at") or time.strftime(
                "%Y-%m-%dT%H:%M:%S%z")
            _atomic_write_json(self._state_path(role), doc)

    def latest_round(self, role: str) -> dict[str, Any] | None:
        rounds = self.load_state(role).get("rounds") or []
        return rounds[-1] if rounds else None

    # ---- 滚动交接 ----

    def write_handoff(self, role: str, doc: dict[str, Any]) -> Path:
        _validate_handoff(doc)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = self.handoff_dir / f"{role}-handoff-{ts}.json"
        _atomic_write_json(path, doc)
        self._prune_handoffs(role)
        return path

    def latest_handoff(self, role: str) -> dict[str, Any] | None:
        files = sorted(self.handoff_dir.glob(f"{role}-handoff-*.json"))
        if not files:
            return None
        return _read_json(files[-1], None)

    def _prune_handoffs(self, role: str) -> None:
        files = sorted(self.handoff_dir.glob(f"{role}-handoff-*.json"))
        for old in files[:-KEEP_HANDOFFS]:
            try:
                old.unlink()
            except OSError:
                pass

    # ---- roster ----

    def load_roster(self) -> dict[str, Any]:
        return _read_json(self.base / "roster.json", {})

    def save_roster(self, doc: dict[str, Any]) -> None:
        _check(doc.get("schema_version") == "duty/roster@0", "schema_version 必须为 duty/roster@0")
        with _file_lock(self.base / "roster.json"):
            _atomic_write_json(self.base / "roster.json", doc)

    # ---- 轮报一行流（替代 rounds.log 的机器侧；人读渲染另出）----

    def log_line(self, line: str) -> None:
        with open(self.base / "rounds.log", "a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
