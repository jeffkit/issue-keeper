"""管线派发互斥 + 后台派发/reaper + 日限语义的回归测试。

来自四起真实事故：

1. **同 issue 重复派发**（#45）：launchd KeepAlive 重启 keeper 时 bridge（独立会话）
   不跟着死，新实例看不见它又派一份 → artifact 目录 `run.lock`（bridge pid）防重。
2. **日限静默消费首次响应**（#19/#23/#41-#44）：日限跳过时置 processed 就再也救不回
   → 日限只推迟、不消费。
3. **同步等整跑堵死轮询**（#40 一夜五轮）：旧 `_invoke_pipeline` 同步等 3000s+，17 个
   仓的轮询全部停摆 → 2026-09-29 派发解耦：`_dispatch_pipeline` 后台拉起（payload 走
   dispatch.json），每轮 `_reap_pipelines` 收尸（读台账终态、补兜底回评、kanban、
   processed、超时清进程组）。
4. **同 issue 每日管线上限**（#40 空转）：终态被拉回队列时无限重派整轮 → 日上限
   跳过且不消费首次响应。
"""

import json
import os
import subprocess
import sys
import time

from issue_keeper.config import Config, RepoBinding
from issue_keeper.keeper import (
    ALREADY_RUNNING,
    _author_over_limit,
    _count_in_flight,
    _dispatch_pipeline,
    _global_pipeline_in_flight,
    _issue_over_pipeline_limit,
    _pid_alive,
    _pipeline_in_flight,
    _reap_pipelines,
    _release_pipeline_lock,
    reopen_issues,
)
from issue_keeper.sources import Resource
from issue_keeper.state import ItemState, State, load_state, save_state


def _res(number: int = 5, author: str = "bob", status: str = "inbox",
         body: str = "正文") -> Resource:
    return Resource(
        kind="issue", number=number, title="t", body=body, state="open",
        labels=[], author=author, created_at="", updated_at="",
        status=status, actor_type="human",
    )


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", ""])
    p.wait()
    return p.pid


def _sleep_pid() -> int:
    """一个活着的进程（reaper 超时清剿测试用）。

    必须 start_new_session：生产的 bridge 就是独立会话，reaper 的 killpg 才只会
    清掉 run 本身；测试里若不隔离，killpg 会把 pytest 自己连着杀掉。"""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    ).pid


def _pipeline_cfg(bridge, **over) -> Config:
    base = dict(
        pipeline_bridge=bridge,
        pipeline_timeout_secs=30,
        pipeline_push_mode="branch",
        pipeline_review_mode="auto",
        pipeline_test_commands={},
    )
    base.update(over)
    return Config(**base)


def _bindings(repo="a/b"):
    return [RepoBinding(repo=repo, profile="p")]


def _art(tmp_path, repo="b", number=7):
    # 目录派生必须与 keeper 一致：slug = repo.split("/")[-1]
    art = tmp_path / ".issue-keeper" / "pipeline" / f"{repo.split('/')[-1]}-{number}"
    art.mkdir(parents=True, exist_ok=True)
    return art


# ── pid 存活判定 ─────────────────────────────────────────────────────

def test_pid_alive_for_self():
    assert _pid_alive(os.getpid()) is True


def test_pid_dead_after_reap():
    assert _pid_alive(_dead_pid()) is False


def test_in_flight_returns_live_holder_and_keeps_lock(tmp_path):
    art = tmp_path / "x"
    art.mkdir(parents=True)
    live = _sleep_pid()
    try:
        (art / "run.lock").write_text(str(live), encoding="utf-8")
        assert _pipeline_in_flight(art) == live
        assert (art / "run.lock").exists()
    finally:
        os.kill(live, 9)


def test_in_flight_clears_stale_lock(tmp_path):
    art = tmp_path / "x"
    art.mkdir(parents=True)
    (art / "run.lock").write_text(str(_dead_pid()), encoding="utf-8")
    assert _pipeline_in_flight(art) is None
    assert not (art / "run.lock").exists()


def test_in_flight_clears_garbage_lock(tmp_path):
    art = tmp_path / "x"
    art.mkdir(parents=True)
    (art / "run.lock").write_text("not-a-pid", encoding="utf-8")
    assert _pipeline_in_flight(art) is None


def test_in_flight_none_when_no_lock(tmp_path):
    art = tmp_path / "x"
    art.mkdir(parents=True)
    assert _pipeline_in_flight(art) is None


def test_release_lock_only_removes_own_pid(tmp_path):
    art = tmp_path / "x"
    art.mkdir(parents=True)
    lock = art / "run.lock"
    lock.write_text("12345", encoding="utf-8")
    _release_pipeline_lock(lock, 999)
    assert lock.exists()  # 不是自己的锁不动
    _release_pipeline_lock(lock, 12345)
    assert not lock.exists()


# ── 后台派发（_dispatch_pipeline）────────────────────────────────────

def test_dispatch_skipped_while_same_issue_in_flight(tmp_path, monkeypatch):
    """锁持有人还活着：不派发、不覆盖在跑 run 的 00-issue.md、不置在途。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text("raise SystemExit(1)\n", encoding="utf-8")
    art = _art(tmp_path)
    (art / "00-issue.md").write_text("在跑 run 的产物", encoding="utf-8")
    (art / "run.lock").write_text(str(os.getpid()), encoding="utf-8")

    it = ItemState()
    out = _dispatch_pipeline(
        _pipeline_cfg(bridge), RepoBinding(repo="a/b", profile="p"),
        _res(number=7), it, "a/b issue#7",
    )

    assert out == {"status": ALREADY_RUNNING, "comment_posted": True}
    assert (art / "00-issue.md").read_text(encoding="utf-8") == "在跑 run 的产物"
    assert it.in_flight_since is None


def test_dispatch_deferred_when_global_slot_taken(tmp_path, monkeypatch):
    """别的 issue 在跑 → 本轮不派发，也不动产物。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text("raise SystemExit(1)\n", encoding="utf-8")
    root = tmp_path / ".issue-keeper" / "pipeline"
    root.mkdir(parents=True)
    (root / ".pipeline.lock").write_text(str(os.getpid()), encoding="utf-8")

    out = _dispatch_pipeline(
        _pipeline_cfg(bridge), RepoBinding(repo="a/b", profile="p"),
        _res(number=7), ItemState(), "a/b issue#7",
    )

    assert out == {"status": ALREADY_RUNNING, "comment_posted": True}
    assert not (root / "b-7" / "00-issue.md").exists()


def test_dispatch_is_detached_and_writes_payload(tmp_path, monkeypatch):
    """派发立即返回；payload 走 dispatch.json（不再依赖 stdin）；锁里写 bridge pid。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "import json, shutil, sys\n"
        "shutil.copy(sys.argv[1], sys.argv[1] + '.seen.json')\n",
        encoding="utf-8",
    )
    art = _art(tmp_path)
    it = ItemState()
    out = _dispatch_pipeline(
        _pipeline_cfg(bridge), RepoBinding(repo="a/b", profile="p"),
        _res(number=7), it, "a/b issue#7",
    )

    assert out["status"] == "dispatched"
    assert it.in_flight_since is not None
    seen_path = art / "dispatch.json.seen.json"
    deadline = time.time() + 5
    while not seen_path.exists() and time.time() < deadline:
        time.sleep(0.05)
    seen = json.loads(seen_path.read_text(encoding="utf-8"))
    assert seen["issue_number"] == 7
    assert seen["worktree_dir"].endswith(".worktrees/issue-7")
    assert (art / "run.lock").exists()


def test_dispatch_posts_claim_comment(tmp_path, monkeypatch):
    """认领评论可关；开着时带 bot marker 发出（多会话撞车的教训）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text("pass\n", encoding="utf-8")
    _art(tmp_path)
    posted = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    _dispatch_pipeline(
        _pipeline_cfg(bridge, pipeline_claim_comment=True),
        RepoBinding(repo="a/b", profile="p"), _res(number=7), ItemState(),
        "a/b issue#7",
    )
    assert posted and "issue-keeper-bot" in posted[0]

    posted.clear()
    _dispatch_pipeline(
        _pipeline_cfg(bridge, pipeline_claim_comment=False),
        RepoBinding(repo="a/b", profile="p"), _res(number=8), ItemState(),
        "a/b issue#8",
    )
    assert posted == []


# ── reaper（_reap_pipelines）────────────────────────────────────────

def _in_flight_state(tmp_path, repo="a/b", number=7, *, live=False, since=None):
    # 注意两个派生不同：state 的 key 是 repo_slug（a-b），artifact 目录是
    # repo.split("/")[-1]（b）——与 keeper 各自的派生保持一致。
    repo_slug = repo.replace("/", "-")
    state = State()
    it = state.repo(repo_slug).item(str(number))
    it.in_flight_since = since if since is not None else time.time()
    art = tmp_path / ".issue-keeper" / "pipeline" / f"{repo.split('/')[-1]}-{number}"
    art.mkdir(parents=True, exist_ok=True)
    (art / "00-issue.md").write_text("正文", encoding="utf-8")
    (art / "run.lock").write_text(
        str(_sleep_pid() if live else _dead_pid()), encoding="utf-8")
    return state, it, art


def _write_issue_ledger(tmp_path, repo: str, number: int, record: dict) -> None:
    ledger = tmp_path / ".issue-keeper" / "pipeline" / "runs.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"repo": repo, "issue": number,
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S+0800"),
                            **record}) + "\n")


def test_reaper_finalizes_done_run(tmp_path, monkeypatch):
    """pid 死了 + 台账 done（回评已发）→ processed、清在途、不重复发兜底。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, art = _in_flight_state(tmp_path, repo="a/b")
    _write_issue_ledger(tmp_path, "a/b", 7, {"status": "done", "comment_posted": True})
    posted = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda *a, **kw: posted.append(a))

    assert _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b")) == 1
    assert it.processed is True
    assert it.in_flight_since is None
    assert not (art / "run.lock").exists()
    assert posted == []


def test_reaper_falls_back_when_no_ledger(tmp_path, monkeypatch):
    """pid 死了且没有台账（bridge 极早崩溃）→ engine_error 兜底回评。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, _art = _in_flight_state(tmp_path, repo="a/b")
    posted = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))

    _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b"))

    assert it.processed is True
    assert len(posted) == 1
    assert "engine_error" in posted[0] and "issue-keeper-bot" in posted[0]


def test_reaper_leaves_live_runs_alone(tmp_path, monkeypatch):
    """pid 活着且未超时：不动（仍在途）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, _art = _in_flight_state(tmp_path, repo="a/b", live=True)
    before = it.in_flight_since

    assert _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b")) == 0
    assert it.in_flight_since == before
    assert it.processed is False


def test_reaper_kills_timed_out_run(tmp_path, monkeypatch):
    """pid 活着但超过 pipeline_timeout_secs → 清进程组 + engine_error 兜底。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, art = _in_flight_state(
        tmp_path, repo="a/b", live=True, since=time.time() - 999)
    posted = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    cfg = _pipeline_cfg(tmp_path / "b", pipeline_timeout_secs=30)

    _reap_pipelines(cfg, state, _bindings(repo="a/b"))

    assert it.processed is True and it.in_flight_since is None
    assert not (art / "run.lock").exists()
    assert posted and "超时" in posted[0]


def test_reaper_records_wakeup_deps_for_blocked(tmp_path, monkeypatch):
    """blocked 终态：正文里引用的依赖编号进 wakeup_deps（#17 教训）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, art = _in_flight_state(tmp_path, repo="a/b")
    (art / "00-issue.md").write_text("依赖 #31 与 #30", encoding="utf-8")
    _write_issue_ledger(tmp_path, "a/b", 7, {"status": "blocked", "comment_posted": True})

    _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b"))

    assert it.wakeup_deps == [31, 30]
    assert it.processed is True


def test_count_in_flight():
    st = State()
    a = st.repo("a").item("1")
    st.repo("a").item("2").in_flight_since = time.time()
    st.repo("b").item("3").in_flight_since = time.time()
    assert _count_in_flight(st) == 2
    assert a.in_flight_since is None


# ── reaper 跨渠道读回 + WIP 快照 + 看板收尾（recursive#2 的三个可修点）──

def test_reaper_readback_suppresses_false_fallback(tmp_path, monkeypatch):
    """台账漏记 comment_posted（#31/#32 的 null），渠道读回确认已回评 → 不补发兜底。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, _art = _in_flight_state(tmp_path, repo="a/b")
    _write_issue_ledger(tmp_path, "a/b", 7,
                        {"status": "blocked", "comment_posted": None, "ok": True})
    posted = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    monkeypatch.setattr("issue_keeper.keeper._channel_reply_posted",
                        lambda *a, **kw: True)

    _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b"))

    assert posted == []
    assert it.processed is True


def test_reaper_readback_failure_keeps_fallback(tmp_path, monkeypatch):
    """渠道读不到（gh 挂/无网）→ 维持兜底回评（fail-safe，不因校验失能而静默）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, _art = _in_flight_state(tmp_path, repo="a/b")
    _write_issue_ledger(tmp_path, "a/b", 7,
                        {"status": "engine_error", "comment_posted": False})
    posted = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    monkeypatch.setattr("issue_keeper.keeper._channel_reply_posted",
                        lambda *a, **kw: False)

    _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b"))

    assert len(posted) == 1 and "engine_error" in posted[0]
    assert it.processed is True


def test_channel_reply_posted_detection_rules(monkeypatch):
    """读回判定：管线标记=硬证据；自己账号非机器评论=兜底；认领/兜底评论不算。"""
    import subprocess
    from issue_keeper.keeper import _channel_reply_posted

    def _comments(lst):
        class _R:
            returncode = 0
            stdout = json.dumps({"comments": lst})
            stderr = ""
        return lambda *a, **kw: _R()

    def _c(body, created, login="keeper-bot"):
        return {"body": body, "createdAt": created,
                "author": {"login": login}}

    # GitHub createdAt 是 UTC（Z）；dispatch 时刻也用 UTC 构造，避免本地时区歧义
    from datetime import datetime, timezone
    since = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc).timestamp()
    after = "2026-09-28T08:05:00Z"
    before = "2026-09-28T07:00:00Z"
    monkeypatch.setattr("issue_keeper.keeper._gh_login", lambda: "keeper-bot")

    # 标记硬证据（不分作者、不论是否机器文本）
    monkeypatch.setattr(subprocess, "run",
                        _comments([_c("<!-- issue-pipeline -->\n说明", after, "someone")]))
    assert _channel_reply_posted("a/b", 7, since, "<!-- issue-keeper-bot -->") is True
    # 自己账号、无标记、非机器文本 → 兜底命中（覆盖无标记的历史回评）
    monkeypatch.setattr(subprocess, "run",
                        _comments([_c("暂不开工，依赖 #5 未合入", after)]))
    assert _channel_reply_posted("a/b", 7, since, "<!-- issue-keeper-bot -->") is True
    # keeper 机器评论（认领/兜底）不算回评
    monkeypatch.setattr(subprocess, "run",
                        _comments([_c("<!-- issue-keeper-bot -->\n[issue-pipeline] 已认领", after)]))
    assert _channel_reply_posted("a/b", 7, since, "<!-- issue-keeper-bot -->") is False
    # 派发之前的评论不算
    monkeypatch.setattr(subprocess, "run",
                        _comments([_c("<!-- issue-pipeline -->\n旧回评", before)]))
    assert _channel_reply_posted("a/b", 7, since, "<!-- issue-keeper-bot -->") is False
    # 别人的普通评论不算
    monkeypatch.setattr(subprocess, "run",
                        _comments([_c("+1", after, "alice")]))
    assert _channel_reply_posted("a/b", 7, since, "<!-- issue-keeper-bot -->") is False


def _init_git_wt(path) -> None:
    """最小 git 仓（带一个基线提交），充当管线 worktree。"""
    path.mkdir(parents=True)

    def g(*args):
        return subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, text=True)

    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (path / "base.txt").write_text("base", encoding="utf-8")
    g("add", "-A")
    g("commit", "-qm", "base")
    return g


def test_snapshot_wip_commits_dirty_worktree(tmp_path):
    from issue_keeper.keeper import _snapshot_worktree_wip
    wt = tmp_path / "wt"
    g = _init_git_wt(wt)
    (wt / "half.txt").write_text("半成品", encoding="utf-8")

    note = _snapshot_worktree_wip(wt, "issue-33")

    assert "已快照" in note
    assert "wip(issue-33)" in g("log", "-1", "--format=%s").stdout
    assert (wt / "half.txt").exists()          # 提交而非丢弃
    assert not (subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip())


def test_snapshot_wip_skips_clean_or_missing(tmp_path):
    from issue_keeper.keeper import _snapshot_worktree_wip
    wt = tmp_path / "wt"
    _init_git_wt(wt)
    assert _snapshot_worktree_wip(wt, "issue-1") == ""     # 干净工作区
    assert _snapshot_worktree_wip(tmp_path / "nope", "issue-1") == ""  # 目录不存在


def test_reaper_snapshots_dirty_worktree_on_engine_error(tmp_path, monkeypatch):
    """引擎异常终止 + worktree 有半成品 → 自动 wip 快照，兜底回评告知位置（#33/#51）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo_dir = tmp_path / "clone"
    wt = repo_dir / ".worktrees" / "issue-7"
    g = _init_git_wt(wt)
    (wt / "half.txt").write_text("Goal 394 半成品", encoding="utf-8")

    state, it, _art = _in_flight_state(tmp_path, repo="a/b")
    _write_issue_ledger(tmp_path, "a/b", 7,
                        {"status": "engine_error", "comment_posted": False})
    posted = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    monkeypatch.setattr("issue_keeper.keeper._channel_reply_posted",
                        lambda *a, **kw: False)

    _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state,
                    [RepoBinding(repo="a/b", profile="p", cwd=str(repo_dir))])

    assert "wip(issue-7)" in g("log", "-1", "--format=%s").stdout
    assert any("快照" in body for body in posted)


def test_reaper_moves_internal_board(tmp_path, monkeypatch):
    """收尸后看板真实移动（原 status_for_board 是死变量）：done+已回评→review，
    引擎异常→todo；不支持状态机的 source 静默跳过。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    moves = []

    class _FakeBoard:
        def move_status(self, repo, resource, to_status, *, actor="", actor_type="human",
                        comment=""):
            moves.append((repo, resource.number, to_status))
            return True, "doing"

    _write_issue_ledger(tmp_path, "a/b", 7, {"status": "done", "comment_posted": True})
    _write_issue_ledger(tmp_path, "a/b", 8,
                        {"status": "engine_error", "comment_posted": False})
    for n in (7, 8):
        state, it, _art = _in_flight_state(tmp_path, repo="a/b", number=n)
        monkeypatch.setattr("issue_keeper.keeper._gh_post_comment", lambda *a, **kw: None)
        monkeypatch.setattr("issue_keeper.keeper._channel_reply_posted",
                            lambda *a, **kw: False)
        monkeypatch.setattr("issue_keeper.keeper._ensure_source",
                            lambda binding, cache: _FakeBoard())
        _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b"))
    assert sorted(moves) == [("a/b", 7, "review"), ("a/b", 8, "todo")]


# ── 日限跳过（事故 2 的回归）────────────────────────────────────────

def _write_author_ledger(tmp_path, author: str) -> None:
    ledger = tmp_path / ".issue-keeper" / "pipeline" / "runs.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    ledger.write_text(
        json.dumps({"ts": f"{today}T10:00:00+0800", "author": author}) + "\n",
        encoding="utf-8",
    )


def test_over_limit_keeps_first_reply_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_author_ledger(tmp_path, "bob")
    assert _author_over_limit(Config(author_daily_limit=1), "bob") is True


def test_exempt_author_is_not_over_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_author_ledger(tmp_path, "okguitar")
    cfg = Config(author_daily_limit=1, author_daily_limit_exempt=["okguitar"])
    assert _author_over_limit(cfg, "okguitar") is False


# ── reopen / 全局槽位 / 同 issue 日上限 ──────────────────────────────

def test_reopen_puts_consumed_issue_back(tmp_path):
    cfg = Config(repos=[RepoBinding(repo="a/b", profile="p")],
                 state_file=tmp_path / "state.json")
    st = State()
    st.repo("a-b").item("5").processed = True
    save_state(cfg.state_path, st)

    assert reopen_issues(cfg, "a/b", [5]) == [5]
    assert load_state(cfg.state_path).repo("a-b").item("5").processed is False


def test_reopen_clears_security_block(tmp_path):
    cfg = Config(repos=[RepoBinding(repo="a/b", profile="p")],
                 state_file=tmp_path / "state.json")
    st = State()
    st.repo("a-b").item("6").blocked = True
    save_state(cfg.state_path, st)

    reopen_issues(cfg, "a/b", [6])
    assert load_state(cfg.state_path).repo("a-b").item("6").blocked is False


def test_reopen_unknown_repo_raises(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        reopen_issues(Config(repos=[RepoBinding(repo="a/b", profile="p")],
                             state_file=tmp_path / "state.json"), "x/y", [1])


def test_global_slot_detects_live_holder(tmp_path):
    root = tmp_path / "p"
    root.mkdir(parents=True)
    live = _sleep_pid()
    try:
        (root / ".pipeline.lock").write_text(str(live), encoding="utf-8")
        assert _global_pipeline_in_flight(root / "b-7") == live
    finally:
        os.kill(live, 9)


def test_global_slot_clears_stale_holder(tmp_path):
    root = tmp_path / "p"
    root.mkdir(parents=True)
    (root / ".pipeline.lock").write_text(str(_dead_pid()), encoding="utf-8")
    assert _global_pipeline_in_flight(root / "b-7") is None
    assert not (root / ".pipeline.lock").exists()


def _write_issue_ledger_n(tmp_path, repo: str, number: int, n: int, day_offset: int = 0) -> None:
    ledger = tmp_path / ".issue-keeper" / "pipeline" / "runs.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT10:00:00+0800",
                       time.localtime(time.time() - day_offset * 86400))
    with ledger.open("a", encoding="utf-8") as f:
        f.writelines([json.dumps({"ts": ts, "repo": repo, "issue": number,
                                  "author": "bob", "status": "engine_error"}) + "\n"] * n)


def test_issue_cap_blocks_redispatch_without_consuming(tmp_path, monkeypatch):
    """终态 issue 被拉回队列时，当日 run 数到顶 → 跳过且不消费首次响应。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_issue_ledger_n(tmp_path, "a/b", 5, n=2)
    assert _issue_over_pipeline_limit(
        Config(pipeline_issue_daily_limit=2), "a/b", 5) is True


def test_issue_cap_counts_same_issue_only(tmp_path, monkeypatch):
    """计数只认同仓同号：别的 issue 跑再多、隔天记录都不占额度。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_issue_ledger_n(tmp_path, "a/b", 5, n=2)
    _write_issue_ledger_n(tmp_path, "a/b", 6, n=5)
    _write_issue_ledger_n(tmp_path, "a/b", 5, n=1, day_offset=1)
    _write_issue_ledger_n(tmp_path, "a/c", 5, n=3)
    cfg = Config(pipeline_issue_daily_limit=2)
    assert _issue_over_pipeline_limit(cfg, "a/b", 5) is True    # 今日本 issue 已 2 次
    assert _issue_over_pipeline_limit(cfg, "a/b", 6) is True    # 今日 5 次
    assert _issue_over_pipeline_limit(cfg, "a/b", 7) is False   # 本 issue 今日 0 次
    assert _issue_over_pipeline_limit(cfg, "a/c", 5) is True


def test_issue_cap_zero_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_issue_ledger_n(tmp_path, "a/b", 5, n=9)
    assert _issue_over_pipeline_limit(
        Config(pipeline_issue_daily_limit=0), "a/b", 5) is False


# ── v0.3 per-repo 契约：资格门控 + payload 构造 ──────────────────────

from issue_keeper.config import GateSpec, PipelineRepoConfig  # noqa: E402
from issue_keeper.keeper import _pipeline_repo_cfg  # noqa: E402


def test_pipeline_repo_cfg_eligibility(tmp_path):
    """无契约/无门 → 不进管线；readonly 无门也进；enabled=false 不进。"""
    cfg = Config(pipeline_repos={
        "a/gated": PipelineRepoConfig(test_command="pytest"),
        "a/ro": PipelineRepoConfig(mode="readonly"),
        "a/off": PipelineRepoConfig(enabled=False, test_command="x"),
    })
    pc, why = _pipeline_repo_cfg(cfg, RepoBinding(repo="a/gated", profile="p"))
    assert pc is not None and pc.test_command == "pytest"
    pc, _ = _pipeline_repo_cfg(cfg, RepoBinding(repo="a/ro", profile="p"))
    assert pc is not None and pc.mode == "readonly"
    pc, why = _pipeline_repo_cfg(cfg, RepoBinding(repo="a/off", profile="p"))
    assert pc is None and "enabled=false" in why
    pc, why = _pipeline_repo_cfg(cfg, RepoBinding(repo="a/other", profile="p"))
    assert pc is None and "无质量门" in why
    # 旧 pipeline_test_commands 兜底（兼容不迁移的仓）
    cfg2 = Config(pipeline_test_commands={"a/legacy": "cargo test"})
    pc, _ = _pipeline_repo_cfg(cfg2, RepoBinding(repo="a/legacy", profile="p"))
    assert pc is not None and pc.test_command == "cargo test"


def _await_seen(art, number: int) -> dict:
    """等 bridge 落盘 dispatch.json.seen.json（后台进程有启动延迟）。"""
    seen_path = art / "dispatch.json.seen.json"
    deadline = time.time() + 5
    while not seen_path.exists() and time.time() < deadline:
        time.sleep(0.05)
    return json.loads(seen_path.read_text(encoding="utf-8"))


def test_dispatch_payload_carries_per_repo_contract(tmp_path, monkeypatch):
    """payload 注入 per-repo 契约：基线/安装/门预算/readonly/notes/push_mode。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "import json, shutil, sys\n"
        "shutil.copy(sys.argv[1], sys.argv[1] + '.seen.json')\n",
        encoding="utf-8",
    )
    art = _art(tmp_path)
    pc = PipelineRepoConfig(
        base_branch="develop",
        setup_command="pnpm install --frozen-lockfile",
        gates=[GateSpec(name="fmt", command="pnpm fmt", timeout_secs=300),
               GateSpec(name="tui", command="x", timeout_secs=3600,
                        paths=["crates/tui/**"])],
        push_mode="pr",
        review_notes="红线 X",
        triage_notes="路由 Y",
        doc_notes="惯例 Z",
        timeout_overrides={"implement": 900},
    )
    out = _dispatch_pipeline(
        _pipeline_cfg(bridge, pipeline_push_mode="main", pipeline_claim_comment=False),  # 全局 main，per-repo pr 覆盖
        RepoBinding(repo="a/b", profile="p"), _res(number=7), ItemState(),
        "a/b issue#7", pc=pc,
    )
    assert out["status"] == "dispatched"
    seen = _await_seen(art, 7)
    assert seen["base_branch"] == "develop"
    assert seen["setup_command"] == "pnpm install --frozen-lockfile"
    assert seen["push_mode"] == "pr"              # per-repo 覆盖全局 main
    assert seen["review_mode"] == "auto"          # 未覆盖 → 继承全局
    assert seen["readonly"] is False
    assert seen["review_notes"] == "红线 X" and seen["triage_notes"] == "路由 Y"
    assert seen["doc_notes"] == "惯例 Z"
    assert seen["gate_timeout_secs"] == 300 + 3600 + 300
    assert seen["implement_timeout"] == 900       # 覆盖生效
    assert seen["investigate_timeout"] == 2100    # 未覆盖用内置默认
    # 多门仓：test_command 指向 gate_runner，spec 落在产物目录
    assert "gate_runner.py" in seen["test_command"]
    spec = json.loads((art / "gates.json").read_text(encoding="utf-8"))
    assert spec["base"] == "develop"
    assert [g["name"] for g in spec["gates"]] == ["fmt", "tui"]
    assert spec["gates"][1]["paths"] == ["crates/tui/**"]


def test_dispatch_payload_readonly_repo(tmp_path, monkeypatch):
    """readonly 仓：readonly=true、无门、不写 gates.json。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "import json, shutil, sys\nshutil.copy(sys.argv[1], sys.argv[1] + '.seen.json')\n",
        encoding="utf-8",
    )
    art = _art(tmp_path)
    pc_ro = PipelineRepoConfig(mode="readonly")
    out = _dispatch_pipeline(
        _pipeline_cfg(bridge, pipeline_claim_comment=False),
        RepoBinding(repo="a/b", profile="p"),
        _res(number=7), ItemState(), "a/b issue#7", pc=pc_ro,
    )
    assert out["status"] == "dispatched"
    seen = _await_seen(art, 7)
    assert seen["readonly"] is True and seen["test_command"] == ""
    assert not (art / "gates.json").exists()


def test_dispatch_payload_single_command_repo(tmp_path, monkeypatch):
    """单命令仓：test_command 原样、默认预算 2400、不写 gates.json。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "import json, shutil, sys\nshutil.copy(sys.argv[1], sys.argv[1] + '.seen.json')\n",
        encoding="utf-8",
    )
    art = _art(tmp_path, number=8)
    pc_single = PipelineRepoConfig(test_command="pnpm test:run")
    out = _dispatch_pipeline(
        _pipeline_cfg(bridge, pipeline_claim_comment=False),
        RepoBinding(repo="a/b", profile="p"),
        _res(number=8), ItemState(), "a/b issue#8", pc=pc_single,
    )
    assert out["status"] == "dispatched"
    seen = _await_seen(art, 8)
    assert seen["test_command"] == "pnpm test:run"
    assert seen["gate_timeout_secs"] == 2400   # 单命令默认预算
    assert not (art / "gates.json").exists()
