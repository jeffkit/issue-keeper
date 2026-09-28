"""pipeline 派发互斥 + 日限跳过语义的回归测试（2026-09-28）。

来自两个真实事故：

1. **同 issue 重复派发**：keeper 由 launchd 托管且 `KeepAlive=true`，重启时
   bridge（`start_new_session`）不会跟着死，变成无主孤儿继续改同一个 worktree；
   新实例看不见它又派一份，于是同一 issue 两个 run 并行（#45 实证：`02-plan.md`
   里留下两份「实施记录」，且残留 run 在 `push_mode=main` 下仍可能往 main 推）。
   修法：artifact 目录放 `run.lock`（内容为 bridge pid），pid 活着就跳过派发。

2. **日限静默消费首次响应**：作者日限跳过时置了 `it.processed = True`，而
   processed 的语义是「后续只看评论」——日限次日重置、豁免名单事后追加都救不回来，
   #19/#23/#41-#44 就是这样一条回评都没有地卡住的。修法：日限只推迟，不消费。
"""

import json
import os
import subprocess
import sys
import time

import pytest

from issue_keeper.config import Config, RepoBinding
from issue_keeper.keeper import (
    ALREADY_RUNNING,
    _author_over_limit,
    _invoke_pipeline,
    _pid_alive,
    _pipeline_in_flight,
    _process_resource,
    _release_pipeline_lock,
    reopen_issues,
)
from issue_keeper.sources import Resource
from issue_keeper.state import RepoState, State, load_state, save_state


def _res(number: int = 5, author: str = "bob", status: str = "inbox",
         body: str = "正文") -> Resource:
    return Resource(
        kind="issue", number=number, title="t", body=body, state="open",
        labels=[], author=author, created_at="", updated_at="",
        status=status, actor_type="human",
    )


class _FakeSrc:
    """只满足 _supports_status 检查（有 move_status 方法）。"""

    def move_status(self, *a, **kw):  # noqa: D401
        return True, "x"


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", ""])
    p.wait()
    return p.pid


def _pipeline_cfg(bridge) -> Config:
    return Config(
        pipeline_bridge=bridge,
        pipeline_timeout_secs=30,
        pipeline_push_mode="branch",
        pipeline_review_mode="auto",
        pipeline_test_commands={},
    )


# ── pid 存活判定 ─────────────────────────────────────────────────────

def test_pid_alive_for_self():
    assert _pid_alive(os.getpid()) is True


def test_pid_dead_after_reap():
    assert _pid_alive(_dead_pid()) is False


# ── 锁的读写与清理 ───────────────────────────────────────────────────

def test_in_flight_returns_live_holder_and_keeps_lock(tmp_path):
    art = tmp_path / "b-7"
    art.mkdir()
    (art / "run.lock").write_text(str(os.getpid()))
    assert _pipeline_in_flight(art) == os.getpid()
    assert (art / "run.lock").exists()


def test_in_flight_clears_stale_lock(tmp_path):
    art = tmp_path / "b-7"
    art.mkdir()
    (art / "run.lock").write_text(str(_dead_pid()))
    assert _pipeline_in_flight(art) is None
    assert not (art / "run.lock").exists()


def test_in_flight_clears_garbage_lock(tmp_path):
    art = tmp_path / "b-7"
    art.mkdir()
    (art / "run.lock").write_text("not-a-pid")
    assert _pipeline_in_flight(art) is None
    assert not (art / "run.lock").exists()


def test_in_flight_none_when_no_lock(tmp_path):
    art = tmp_path / "b-7"
    art.mkdir()
    assert _pipeline_in_flight(art) is None


def test_release_lock_only_removes_own_pid(tmp_path):
    lock = tmp_path / "run.lock"
    lock.write_text("123456")
    _release_pipeline_lock(lock, os.getpid())   # 不是自己写的
    assert lock.exists()
    _release_pipeline_lock(lock, 123456)
    assert not lock.exists()


# ── 派发互斥（事故 1 的回归）─────────────────────────────────────────

def test_dispatch_skipped_while_same_issue_in_flight(tmp_path, monkeypatch):
    """锁持有人还活着：不派发、不覆盖在跑 run 的 00-issue.md。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text("raise SystemExit(1)\n", encoding="utf-8")
    art = tmp_path / ".issue-keeper" / "pipeline" / "b-7"
    art.mkdir(parents=True)
    (art / "00-issue.md").write_text("在跑 run 的产物", encoding="utf-8")
    (art / "run.lock").write_text(str(os.getpid()), encoding="utf-8")

    out = _invoke_pipeline(
        _pipeline_cfg(bridge), RepoBinding(repo="a/b", profile="p"),
        _res(number=7), "a/b issue#7",
    )

    assert out == {"status": ALREADY_RUNNING, "comment_posted": True}
    assert (art / "00-issue.md").read_text(encoding="utf-8") == "在跑 run 的产物"


def test_stale_lock_does_not_block_dispatch(tmp_path, monkeypatch):
    """锁持有人已退出：视为陈旧锁，正常派发并在结束后释放锁。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print('RESULT ' + json.dumps({'status': 'done', 'comment_posted': True}))\n",
        encoding="utf-8",
    )
    art = tmp_path / ".issue-keeper" / "pipeline" / "b-7"
    art.mkdir(parents=True)
    (art / "run.lock").write_text(str(_dead_pid()), encoding="utf-8")

    out = _invoke_pipeline(
        _pipeline_cfg(bridge), RepoBinding(repo="a/b", profile="p", cwd=str(tmp_path)),
        _res(number=7), "a/b issue#7",
    )

    assert out == {"status": "done", "comment_posted": True}
    assert (art / "00-issue.md").exists()
    assert not (art / "run.lock").exists()


# ── 日限跳过（事故 2 的回归）────────────────────────────────────────

def _write_ledger(tmp_path, author: str) -> None:
    ledger = tmp_path / ".issue-keeper" / "pipeline" / "runs.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    ledger.write_text(
        json.dumps({"ts": f"{today}T10:00:00+0800", "author": author}) + "\n",
        encoding="utf-8",
    )


def test_over_limit_keeps_first_reply_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_ledger(tmp_path, "bob")
    cfg = Config(author_allowlist=[], author_daily_limit=1, author_daily_limit_exempt=[])
    rs = RepoState()

    handled = _process_resource(
        _FakeSrc(), RepoBinding(repo="a/b", profile="p"), cfg, cfg.screener,
        None, rs, _res(author="bob"), "", 60, "[issue-keeper:x]",
    )

    assert handled == 0
    assert rs.item("5").processed is False   # 关键：日限只推迟，不消费首次响应


def test_exempt_author_is_not_over_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_ledger(tmp_path, "bob")
    cfg = Config(author_daily_limit=1, author_daily_limit_exempt=["BOB"])
    assert _author_over_limit(cfg, "bob") is False


def test_allowlist_skip_still_consumes_first_reply(tmp_path, monkeypatch):
    """allowlist 是永久性拒绝：保持原语义（消费掉，不再反复扫）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config(author_allowlist=["alice"], author_daily_limit=1)
    rs = RepoState()

    _process_resource(
        _FakeSrc(), RepoBinding(repo="a/b", profile="p"), cfg, cfg.screener,
        None, rs, _res(author="bob"), "", 60, "[issue-keeper:x]",
    )

    assert rs.item("5").processed is True


# ── 人工重派入口 reopen（同一次事故的结构性缺口）────────────────────
# 日限误跳过/引擎异常之后，keeper 原先没有任何手段让一条 processed 的 issue
# 再进队，只能绕过 keeper 跑 ad-hoc 脚本（还会丢掉兜底回评）。

def test_reopen_puts_consumed_issue_back(tmp_path):
    cfg = Config(repos=[RepoBinding(repo="a/b", profile="p")],
                 state_file=tmp_path / "state.json")
    st = State()
    it = st.repo("a-b").item("5")
    it.processed = True
    it.processed_comment_ids.add("IC_1")
    save_state(cfg.state_path, st)

    changed = reopen_issues(cfg, "a/b", [5, 6])

    assert changed == [5]                      # 6 号本就没被消费，不动
    it2 = load_state(cfg.state_path).repo("a-b").item("5")
    assert it2.processed is False
    assert "IC_1" in it2.processed_comment_ids  # 评论级进度保留，旧评论不重答


def test_reopen_clears_security_block(tmp_path):
    """被 screener 拦下的 issue 人工重派：放开重新走一遍（正文会重新过闸）。"""
    cfg = Config(repos=[RepoBinding(repo="a/b", profile="p")],
                 state_file=tmp_path / "state.json")
    st = State()
    st.repo("a-b").item("9").blocked = True
    save_state(cfg.state_path, st)

    assert reopen_issues(cfg, "a/b", [9]) == [9]
    assert load_state(cfg.state_path).repo("a-b").item("9").blocked is False


def test_reopen_unknown_repo_raises(tmp_path):
    cfg = Config(repos=[RepoBinding(repo="a/b", profile="p")],
                 state_file=tmp_path / "state.json")
    with pytest.raises(ValueError):
        reopen_issues(cfg, "x/y", [1])
