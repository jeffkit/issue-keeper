"""issue #12：engine_error 首败自动重派补一轮冷却 + 认领评论抑制键泛化。

契约（acceptance，见 00-issue.md §验收）：

- 三支自动重派（retry-later / failed 类 / engine_error）退避写入点同一语义：
  写 `it.retry_after = now + max(1, int(config.poll_interval_secs))`
  （retry-later 按 #6 的指数档位，首连 = poll_interval）；
- 退避未到时 `_process_resource` 不派发；
- 「本轮为自动重派」期间不贴认领评论；首派（非自动重派）仍恰贴一条且带 bot_marker；
- 终态收尾仍清 `retry_after=None` + `retry_later_streak=0`（#6/#8 不回归）。

判别力：`retry_after` 一旦不写就是零冷却（`run_once` 先收尸再扫仓，同轮即重派），
故第 1 节按 `AUTO_RETRY_STATUSES` 参数化三支逐一断言；参数化是必需的——只测
engine_error 无法发现另两支被改坏。第 2 节用真实 `_process_resource` +
真实 `_dispatch_pipeline`，只观察公屏可见行为，不绑定「本轮为自动重派」由哪个
字段承载。
"""
from __future__ import annotations

import time

import pytest

from issue_keeper.keeper import (
    _dispatch_pipeline,
    _process_resource,
    _reap_pipelines,
)
from issue_keeper.state import ItemState
from tests.test_failed_escalation import (
    _defer_env,
    _ledger_row,
    _pipeline_on_cfg,
)
from tests.test_pipeline_dispatch_guard import (
    _art,
    _bindings,
    _in_flight_state,
    _pipeline_cfg,
    _res,
)

POLL = 420          # 实盘 config.yaml 的 poll_interval_secs（7min）
AUTO_RETRY_STATUSES = ("retry-later", "failed", "engine_error")


@pytest.fixture
def reap_env(tmp_path, monkeypatch):
    """reaper 隔离：HOME → tmp；回评与渠道读回桩掉，全部捕获。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, art = _in_flight_state(tmp_path, repo="a/b")
    posted: list[str] = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    monkeypatch.setattr("issue_keeper.keeper._channel_reply_posted",
                        lambda *a, **kw: False)
    return state, it, art, posted, tmp_path


# ── 1. 三支退避写入点语义一致：都写一轮 ≥ poll_interval 的冷却 ──────────────

@pytest.mark.parametrize("status", AUTO_RETRY_STATUSES)
def test_三支自动重派都写一轮冷却(reap_env, status):
    """判别力：engine_error 分支修复前不写 retry_after → 本参数化在该档位红。

    只清 `in_flight_since` 等于零冷却——`run_once` 同轮就会扫到并重派。
    """
    state, it, art, posted, home = reap_env
    _ledger_row(home, "a/b", 7, status=status, comment_posted=False, error="boom")
    t0 = time.time()

    _reap_pipelines(_pipeline_cfg(home / "b", poll_interval_secs=POLL),
                    state, _bindings(repo="a/b"))

    assert it.processed is False, f"{status} 自动重派不消费首次响应"
    assert it.in_flight_since is None, "清在途锚，退避到期后才可重派"
    assert posted == [], "自动重派的收尸阶段不发评论（升级才发）"
    assert it.retry_after is not None and it.retry_after > time.time(), (
        f"{status} 自动重派必须写退避截止（复用 it.retry_after），"
        "否则先收尸再扫仓的同一轮里立刻重派 = 无冷却")
    assert it.retry_after - t0 == pytest.approx(POLL, abs=5), (
        f"{status} 冷却档位应为一轮 poll_interval（{POLL}s）")


def test_engine_error退避未到不派发_到点才派发(reap_env, monkeypatch):
    """判别力：修复前该分支 retry_after 为 None → 首次 `_process_resource` 就派发 → 红。"""
    state, it, art, posted, home = reap_env
    _ledger_row(home, "a/b", 7, status="engine_error", comment_posted=False, error="boom")

    _reap_pipelines(_pipeline_cfg(home / "b", poll_interval_secs=POLL),
                    state, _bindings(repo="a/b"))

    dispatched: list = []
    monkeypatch.setattr("issue_keeper.keeper._dispatch_pipeline",
                        lambda *a, **kw: dispatched.append(kw) or {"status": "dispatched"})
    env = _defer_env(_pipeline_on_cfg(poll_interval_secs=POLL), rs=state.repo("a-b"))

    assert _process_resource(**env) == 0, (
        "engine_error 自动重派必须经过一轮冷却，同轮不得再派")
    assert dispatched == []

    it.retry_after = time.time() - 1        # 退避到点
    assert _process_resource(**env) == 1
    assert len(dispatched) == 1, "到点后正常重派"


# ── 2. 认领评论：自动重派轮不再贴；首派恰一条 ───────────────────────────────

@pytest.mark.parametrize("status", AUTO_RETRY_STATUSES)
def test_自动重派轮不贴认领评论(status, tmp_path, monkeypatch):
    """判别力：修复前 failed / engine_error 两档照贴（streak 仍是 0）→ 红。

    重派走真实 `_process_resource` + 真实 `_dispatch_pipeline`（不桩派发函数），
    只观察公屏可见行为——不绑定「本轮为自动重派」用什么字段承载。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, art = _in_flight_state(tmp_path, repo="a/b")
    posted: list[str] = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    monkeypatch.setattr("issue_keeper.keeper._channel_reply_posted",
                        lambda *a, **kw: False)
    _ledger_row(tmp_path, "a/b", 7, status=status, comment_posted=False, error="boom")

    _reap_pipelines(_pipeline_cfg(tmp_path / "b", poll_interval_secs=POLL),
                    state, _bindings(repo="a/b"))
    assert posted == [], "收尸阶段不发评论"

    bridge = tmp_path / "bridge.py"
    bridge.write_text("pass\n", encoding="utf-8")
    _art(tmp_path)
    it.retry_after = time.time() - 1        # 退避到点，本轮自动重派
    cfg = _pipeline_on_cfg(pipeline_bridge=str(bridge), pipeline_claim_comment=True,
                           poll_interval_secs=POLL)

    assert _process_resource(**_defer_env(cfg, rs=state.repo("a-b"))) == 1

    assert posted == [], (
        f"{status} 自动重派是同一 chain 的续跑，不得再贴「已认领本 issue 开始处理」"
        "——连续故障期间公屏认领评论恒 ≤1 条（#6 的抑制语义泛化到三支）")


def test_首派恰贴一条认领评论并保留bot_marker(tmp_path, monkeypatch):
    """负向哨兵：抑制键泛化后，真正的首派（非自动重派）仍要贴，且带 marker。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text("pass\n", encoding="utf-8")
    _art(tmp_path)
    posted: list[str] = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))

    _dispatch_pipeline(_pipeline_cfg(bridge, pipeline_claim_comment=True),
                       _bindings(repo="a/b")[0], _res(number=12), ItemState(),
                       "a/b issue#12")

    assert len(posted) == 1, "首派恰贴一条认领评论"
    assert "已认领" in posted[0]
    assert "issue-keeper-bot" in posted[0], "防循环第一层 marker 不得丢"


# ── 3. 终态收尾不回归（#6/#8 既有契约哨兵）────────────────────────────────

def test_终态收尾仍清零退避与连击(reap_env):
    """二连 engine_error 升级收尾：processed 且 retry_after/streak 清零。"""
    state, it, art, posted, home = reap_env
    for _ in range(2):
        _ledger_row(home, "a/b", 7, status="engine_error", comment_posted=False, error="boom")

    _reap_pipelines(_pipeline_cfg(home / "b", poll_interval_secs=POLL),
                    state, _bindings(repo="a/b"))

    assert it.processed is True, "二连升级即终态"
    assert it.in_flight_since is None
    assert it.retry_after is None, "终态不留幽灵退避"
    assert int(getattr(it, "retry_later_streak", 0) or 0) == 0, "终态清零连击"
