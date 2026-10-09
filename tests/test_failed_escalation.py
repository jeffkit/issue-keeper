"""issue #8：failed（评审两轮不过）自动重试 / 连击升级 / 日限解耦与可观测。

修复前的行为（本文件在基线上应全红）：

1. reaper 只有 retry-later / engine_error 两条自动分支，`status == "failed"`
   落入无条件收尾 → `it.processed = True`：一次内容性失败就把 issue 消费掉，
   只能人工 `reopen`。
2. `_consecutive_engine_errors` 遇非 engine_error 即 break，failed 行既没有
   自己的连击计数，还会把 engine_error 连击清零。
3. 日限把 failed 计入（`_issue_over_pipeline_limit` 只豁免 retry-later）→
   人工 reopen 后当天额度已被失败 run 烧掉，超额只留 INFO，页面上看不到
   「为什么这条不动」。

既有语义（回归哨兵）：off-by-one 契约——台账终态行在计数**之前**已落盘，
尾部连续数已含本次，决策处不得 +1（首败 1 → 重试，二连 2 → 升级）。
"""
from __future__ import annotations

import json
import logging
import pathlib
import subprocess
import time
import types

import pytest

from issue_keeper.config import Config, PipelineRepoConfig, RepoBinding, ScreenerConfig
from issue_keeper.keeper import (
    _author_over_limit,
    _consecutive_engine_errors,
    _issue_over_pipeline_limit,
    _process_resource,
    _reap_pipelines,
)
from issue_keeper.sources import Resource
from issue_keeper.state import load_state, save_state, save_state_item, save_state_merged
from tests.test_pipeline_dispatch_guard import (
    _bindings,
    _in_flight_state,
    _pipeline_cfg,
)

# 失败类（内容性失败）：issue #8 要求它们共享「近 12h 连击 ≥2 → 升级」的语义
FAILURE_STATUSES = ("failed", "guarded", "partial")
# 自动重派只对 failed 生效（acceptance 原文：「首次 failed 自动重派一轮」）
RETRY_STATUSES = ("failed",)


def _ledger_row(home: pathlib.Path, repo: str, number: int, **rec) -> None:
    ledger = home / ".issue-keeper" / "pipeline" / "runs.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    row = {"repo": repo, "issue": number,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S+0800"), **rec}
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _screener() -> ScreenerConfig:
    return ScreenerConfig(enabled=False, provider="openai", api_key=None,
                          base_url=None, model=None, on_unsafe="skip", max_chars=8000)


def _res(number: int = 7, status: str = "inbox", body: str = "正文") -> Resource:
    return Resource(kind="issue", number=number, title="t", body=body, state="open",
                    labels=[], author="bob", created_at="", updated_at="",
                    status=status, actor_type="human")


def _rs(in_flight_since=None, processed=False):
    def item(key):
        return types.SimpleNamespace(
            in_flight_since=in_flight_since, processed=processed, blocked=False,
            processed_comment_ids=set(), session_id=None, comment_tasks={})
    return types.SimpleNamespace(item=item)


@pytest.fixture
def ledger_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def reap_env(tmp_path, monkeypatch):
    """reaper 隔离：HOME → tmp；兜底回评与渠道读回桩掉；gh 调用全部捕获。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, art = _in_flight_state(tmp_path, repo="a/b")
    posted = []
    gh_calls: list[list] = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    monkeypatch.setattr("issue_keeper.keeper._channel_reply_posted",
                        lambda *a, **kw: False)

    def _fake_run(cmd, *a, **kw):
        gh_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return state, it, art, posted, gh_calls, tmp_path


def _label_calls(gh_calls: list[list]) -> list[list]:
    return [c for c in gh_calls
            if "--add-label" in [str(x) for x in c]]


# ── 1. failed_auto_retry：首次失败自动重派一轮，不消费首次响应 ──────────────

@pytest.mark.parametrize("status", RETRY_STATUSES)
def test_首次failed不消费_自动重派一轮(reap_env, status):
    """首败（近 12h 内单行）→ 清在途/清锁、不置 processed，下一轮自动重派。

    `failed_auto_retry` 默认 1。基线行为：无条件 `processed = True` → 断言失败。
    """
    state, it, art, posted, gh_calls, home = reap_env
    _ledger_row(home, "a/b", 7, status=status, comment_posted=True,
                error="review did not pass after one fix round")

    _reap_pipelines(_pipeline_cfg(home / "b"), state, _bindings(repo="a/b"))

    assert it.processed is False, (
        "首次失败必须自动重派一轮（failed_auto_retry 默认 1），不得消费首次响应")
    assert it.in_flight_since is None, "清在途锚，下一轮才可重派"
    assert not (art / "run.lock").exists(), "锁要清掉，否则 ALREADY_RUNNING 挡住重派"
    assert posted == [], "重试阶段不发评论（升级才发）"
    assert _label_calls(gh_calls) == [], "重试阶段不打 needs-human 标签"


def test_重试耗尽才升级_二连失败发评论打标签(reap_env):
    """近 12h 内连续 2 次失败 → 升级：发升级评论 + needs-human 标签 + 终态。"""
    state, it, art, posted, gh_calls, home = reap_env
    for _ in range(2):
        _ledger_row(home, "a/b", 7, status="failed", comment_posted=True,
                    error="review did not pass after one fix round")

    _reap_pipelines(_pipeline_cfg(home / "b"), state, _bindings(repo="a/b"))

    assert it.processed is True, "升级即终态收尾（不再自动重派）"
    assert it.in_flight_since is None
    assert len(posted) == 1, "升级必须发一条评论（恰好一条，别和兜底回评叠双）"
    assert "failed" in posted[0] or "失败" in posted[0]
    assert "issue-keeper-bot" in posted[0], "升级评论必带 bot marker（防循环第一层）"
    labels = _label_calls(gh_calls)
    assert labels, "升级必须给 issue 打 needs-human 标签"
    assert any("needs-human" in [str(x) for x in c] for c in labels)


def test_engine_error二连升级_发评论打标签(reap_env):
    """engine_error 连续 2 次 → 升级：发升级评论 + needs-human 标签。

    2026-10-09 缺口修复的护栏：本分支此前**只落 keeper 日志**，GitHub 上零痕迹
    （实证 recursive#86 / argusai#13 无标签无评论，而走 failed 路径的 hitl-mcp#4 有），
    导致外部看不到、值守与看板也无法发现「哪些单在等人」。修复前本用例必然失败。
    """
    state, it, art, posted, gh_calls, home = reap_env
    # comment_posted=False 是本补丁覆盖的路径（分支闸 `not posted`）；
    # posted=True（已有终态回评）时整个 engine_error 升级分支不进入——另一处缺口，另行决策
    for _ in range(2):
        _ledger_row(home, "a/b", 7, status="engine_error", comment_posted=False,
                    error="executor 'recursive' exited 1")

    _reap_pipelines(_pipeline_cfg(home / "b"), state, _bindings(repo="a/b"))

    assert it.processed is True, "engine_error 二连升级即终态收尾（不再自动重派）"
    assert it.in_flight_since is None
    assert len(posted) == 1, "升级必须发一条评论（恰好一条，别和兜底回评叠双）"
    assert "engine_error" in posted[0] or "引擎级失败" in posted[0]
    assert "issue-keeper-bot" in posted[0], "升级评论必带 bot marker（防循环第一层）"
    labels = _label_calls(gh_calls)
    assert labels, "engine_error 升级必须给 issue 打 needs-human 标签"
    assert any("needs-human" in [str(x) for x in c] for c in labels)


def test_guarded与partial同样走连击升级(reap_env):
    """issue #8 的连击集合含 guarded/partial（均为「内容性失败」）。"""
    state, it, art, posted, gh_calls, home = reap_env
    _ledger_row(home, "a/b", 7, status="guarded", comment_posted=True)
    _ledger_row(home, "a/b", 7, status="partial", comment_posted=True)

    _reap_pipelines(_pipeline_cfg(home / "b"), state, _bindings(repo="a/b"))

    assert it.processed is True
    assert len(posted) == 1, "guarded→partial 二连必须升级"
    assert _label_calls(gh_calls), "升级必须打 needs-human 标签"


# ── 2. 连击计数：failed 不再打断 engine_error；新增失败类计数 ────────────────

def test_failed行不打断engine_error连击(ledger_home):
    """engine_error 中间夹一条 failed：连击不清零（基线 break 在 failed 行）。"""
    _ledger_row(ledger_home, "a/b", 7, status="engine_error")
    _ledger_row(ledger_home, "a/b", 7, status="failed")
    _ledger_row(ledger_home, "a/b", 7, status="engine_error")

    assert _consecutive_engine_errors("a/b", 7) == 2

    # 语义护栏：真正的「未开跑/在途」行仍须打断（dispatch 行 status=None）
    _ledger_row(ledger_home, "a/b", 8, status="engine_error")
    _ledger_row(ledger_home, "a/b", 8, status=None)
    _ledger_row(ledger_home, "a/b", 8, status="engine_error")
    assert _consecutive_engine_errors("a/b", 8) == 1


def test_失败连击计数_含guarded_partial_且12h窗(ledger_home):
    """`_consecutive_failures`：failed/guarded/partial 的近 12h 尾部连击数。"""
    import issue_keeper.keeper as K
    fn = getattr(K, "_consecutive_failures", None)
    assert fn is not None, "缺少失败类连击计数（_consecutive_failures，12h 窗）"

    _ledger_row(ledger_home, "a/b", 7, status="guarded")
    _ledger_row(ledger_home, "a/b", 7, status="failed")
    _ledger_row(ledger_home, "a/b", 7, status="partial")
    assert fn("a/b", 7) == 3

    # 12h 窗外的失败出局（同 _consecutive_engine_errors 的窗口语义）
    old = time.strftime("%Y-%m-%dT%H:%M:%S+0800",
                        time.localtime(time.time() - 20 * 3600))
    ledger = ledger_home / ".issue-keeper" / "pipeline" / "runs.jsonl"
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"repo": "a/b", "issue": 9, "ts": old,
                            "status": "failed"}) + "\n")
    _ledger_row(ledger_home, "a/b", 9, status="failed")
    assert fn("a/b", 9) == 1

    # done 行收束连击（成功即清零）
    _ledger_row(ledger_home, "a/b", 9, status="done")
    assert fn("a/b", 9) == 0


# ── 3. 日限：对 failed 的计入与 reopen 配额解耦 + 超额可观测 ────────────────

def _write_today_rows(home: pathlib.Path, status: str, n: int,
                      repo: str = "a/b", number: int = 7, author: str = "bob") -> None:
    for _ in range(n):
        _ledger_row(home, repo, number, status=status, author=author)


def test_failed行不占日限与作者配额(ledger_home):
    """失败类 run 不再消耗「人工 reopen 才能用的」日限额度。

    现状：`_issue_over_pipeline_limit` 只豁免 retry-later，`_author_over_limit`
    更是按作者计数不分状态 → 失败尝试把当天额度烧光。失败类有自己的一套预算
    （failed_auto_retry + 连击升级即终态），安全边界不依赖日限。
    """
    _write_today_rows(ledger_home, "failed", 5)
    cfg = Config(pipeline_issue_daily_limit=2, author_daily_limit=1)
    assert _issue_over_pipeline_limit(cfg, "a/b", 7) is False, "failed 行不占 issue 日限"
    assert _author_over_limit(cfg, "bob") is False, "failed 行不占作者日限"

    # 真实 run 记录仍照常计入（防 #40 空转的额度语义不变）
    _write_today_rows(ledger_home, "engine_error", 2, repo="a/c", number=8)
    assert _issue_over_pipeline_limit(cfg, "a/c", 8) is True


def _defer_env(cfg: Config, rs=None):
    """把 _process_resource 推到「日限推迟」分支并捕获日志。"""
    return dict(
        src=types.SimpleNamespace(move_status=lambda *a, **k: (True, "x"),
                                  list_comments=lambda *a, **k: [],
                                  web_url=lambda *a, **k: "http://x"),
        binding=RepoBinding(repo="a/b", profile="p"),
        config=cfg,
        screener=_screener(),
        entry=None,
        rs=rs or _rs(),
        res=_res(7),
        me="ik",
        timeout=60,
        visible_prefix="[ik]",
    )


def _pipeline_on_cfg(**over) -> Config:
    base = dict(pipeline_mode=True,
                pipeline_repos={"a/b": PipelineRepoConfig(test_command="pytest")},
                pipeline_issue_daily_limit=2,
                author_daily_limit=0,      # 隔离作者闸，只测 issue 日限
                opt_out_labels=["keeper-ignore"],
                bot_marker="<!-- issue-keeper-bot -->")
    base.update(over)
    return Config(**base)


def test_issue日限超额推迟发WARNING_not_INFO(ledger_home, caplog):
    """超额推迟必须可观测（WARNING 及以上）——「这条为什么不跑」不能只在 INFO 里。"""
    _write_today_rows(ledger_home, "engine_error", 2)
    with caplog.at_level(logging.WARNING, logger="issue-keeper"):
        handled = _process_resource(**_defer_env(_pipeline_on_cfg()))

    assert handled == 0
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "issue 日限超额推迟必须发 WARNING（可观测告警），基线只留 INFO")


def test_作者日限超额推迟发WARNING_not_INFO(ledger_home, caplog):
    """作者日限路径同款：超额推迟不再是静默 INFO。"""
    _write_today_rows(ledger_home, "engine_error", 3, repo="a/other", number=11)
    cfg = _pipeline_on_cfg(author_daily_limit=1)
    with caplog.at_level(logging.WARNING, logger="issue-keeper"):
        handled = _process_resource(**_defer_env(cfg))

    assert handled == 0
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "作者日限超额推迟必须发 WARNING（可观测告警），基线只留 INFO")


# ── 4. 退避：重试不是「失败即立刻再烧一整轮」 ───────────────────────────────

def test_首败退避_未到点不派发_到点放行(reap_env, monkeypatch):
    """reaper 判重试后写 retry_after；退避期间 _process_resource 不派发。"""
    state, it, art, posted, gh_calls, home = reap_env
    _ledger_row(home, "a/b", 7, status="failed", comment_posted=True, error="评审未过")

    _reap_pipelines(_pipeline_cfg(home / "b"), state, _bindings(repo="a/b"))

    assert it.retry_after and it.retry_after > time.time(), "首败重试必须带退避"

    item = types.SimpleNamespace(
        in_flight_since=None, processed=False, blocked=False, processed_comment_ids=set(),
        session_id=None, comment_tasks={}, retry_after=it.retry_after)
    rs = types.SimpleNamespace(item=lambda key: item)
    dispatched: list = []
    monkeypatch.setattr("issue_keeper.keeper._dispatch_pipeline",
                        lambda *a, **kw: dispatched.append(kw) or {"status": "dispatched"})

    env = _defer_env(_pipeline_on_cfg(), rs=rs)
    assert _process_resource(**env) == 0
    assert dispatched == [], "退避未到点不得派发"

    item.retry_after = time.time() - 1        # 退避到点
    assert _process_resource(**env) == 1
    assert len(dispatched) == 1, "到点后正常派发（台账将 +1 run 记录）"


@pytest.mark.parametrize("budget,n_rows,retried", [
    (1, 1, True),      # 首败（budget=1）→ 重试
    (1, 2, False),     # 额度耗尽 → 升级
    (2, 2, True),      # budget=2：连击 2 仍在额度内
    (2, 3, False),     # 第 3 连才升级
])
def test_failed_auto_retry额度参数化(reap_env, budget, n_rows, retried):
    """阈值契约：重试 iff n_fail <= budget；升级 iff n_fail >= 2 and n_fail > budget。"""
    state, it, art, posted, gh_calls, home = reap_env
    for _ in range(n_rows):
        _ledger_row(home, "a/b", 7, status="failed", comment_posted=True, error="评审未过")

    _reap_pipelines(_pipeline_cfg(home / "b", failed_auto_retry=budget),
                    state, _bindings(repo="a/b"))

    if retried:
        assert it.processed is False, f"budget={budget} 连击 {n_rows} 仍应重试"
        assert it.retry_after and it.retry_after > time.time()
        assert posted == []
    else:
        assert it.processed is True, f"budget={budget} 连击 {n_rows} 应升级终态"
        assert len(posted) == 1
        assert _label_calls(gh_calls)


def test_retry_after持久化与合并写(tmp_path):
    """#9 合并写语义：state.py 四处漏一处即被静默丢弃，故单独回归。"""
    path = tmp_path / "state.json"
    seed = load_state(path)
    seed.repo("a-b").item("7")
    save_state(path, seed)                      # 盘上：a-b/7，retry_after=None
    base = load_state(path)                     # daemon 轮首快照
    mem = load_state(path)                      # 轮内工作副本
    save_state_item(path, "a-b", "7",
                    lambda it: setattr(it, "retry_after", 500.0))   # 轮内 reaper 写退避
    assert load_state(path).repo("a-b").item("7").retry_after == 500.0

    mem.repo("a-b").item("7").processed = True
    save_state_merged(path, mem, base)

    disk = load_state(path).repo("a-b").item("7")
    assert disk.retry_after == 500.0, "盘上轮内写入的退避不得被整轮旧快照反盖"
    assert disk.processed is True


def test_engine_error已有终态回评仍升级(reap_env):
    """comment_posted=True（终态回评已发）时，engine_error 二连仍必须升级。

    修复前 `not posted` 闸让整个升级分支不进入（既无评论也无标签）→ 人工队列在
    GitHub 上不可见（recursive#86 / argusai#13 走 engine_error 路径无标签；
    对照 hitl-mcp#4 走 failed 路径有标签）。修复后：升级评论 + needs-human 标签。
    """
    state, it, art, posted, gh_calls, home = reap_env
    for _ in range(2):
        _ledger_row(home, "a/b", 7, status="engine_error", comment_posted=True,
                    error="executor 'recursive' exited 1")

    _reap_pipelines(_pipeline_cfg(home / "b"), state, _bindings(repo="a/b"))

    labels = _label_calls(gh_calls)
    assert labels, "已有终态回评也必须打 needs-human 标签（人工队列可见性）"
    assert any("needs-human" in [str(x) for x in c] for c in labels)
    assert any("引擎级失败" in p for p in posted), "必须发升级求助评论（语义区别于终态说明）"
