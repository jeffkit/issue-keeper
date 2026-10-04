"""issue #6 回归：retry-later 空转的指数退避 + 空转重派不补发认领评论。

修复前的缺陷（基线 e765463）：

1. `_reap_pipelines` 的 retry-later 分支只清 `in_flight_since`，不写任何退避锚
   → 同轮 `process_repo` 立刻再派（`run_once` 先 reap 再扫仓），重派节奏 =
   poll_interval_secs（实盘 7min）。日限豁免（2b693af）后唯一刹车也没了
   → 磁盘守卫风暴下每 issue 每 7min 一条认领评论 + 一次短命 bridge（10-04 实证
   recursive#87 一小时 10 条）。
2. `_dispatch_pipeline` 无条件发「已认领开始处理」→ 评论数随轮次线性增长。

契约（acceptance）：
- state 记 `retry_later_streak`（连击数）+ 复用既有 `it.retry_after` 作退避截止；
  间隔按 7min（= poll_interval_secs）→ 1h → 6h 封顶，单调递增。
- 退避未到期的轮次不派发（`_process_resource` 现有 retry_after 闸，keeper.py:442-445）。
- retry-later 重派路径不补发认领评论（streak > 0 即空转重派）；同一 issue 连续
  retry-later 期间认领评论 ≤ 1 条。
- 终态收尾/`reopen` 清零 streak，退避重新从 7min 起算。
- 日限豁免语义不变（retry-later 既不计 issue 日限也不计作者日限）。
"""
from __future__ import annotations

import time

import pytest

from issue_keeper.config import Config
from issue_keeper.keeper import (
    _author_over_limit,
    _clear_terminal_state,
    _dispatch_pipeline,
    _issue_over_pipeline_limit,
    _process_resource,
    _reap_pipelines,
)
from issue_keeper.state import ItemState, load_state, save_state
from tests.test_failed_escalation import (
    _defer_env,
    _ledger_row,
    _pipeline_on_cfg,
)
from tests.test_pipeline_dispatch_guard import (
    _art,
    _bindings,
    _dead_pid,
    _in_flight_state,
    _pipeline_cfg,
    _res,
)

PROD_POLL = 420          # 实盘 config.yaml 的 poll_interval_secs（7min）
BACKOFF_1H = 3600
BACKOFF_CAP = 21600      # 6h 上限


def _retry_later_row(home, status="retry-later", error="disk low watermark"):
    """播一条本轮 bridge 写的台账行（reaper 据此判终态）。"""
    _ledger_row(home, "a/b", 7, status=status, comment_posted=True, error=error)


def _arm_in_flight(it, art) -> None:
    """让条目重新「在途」：reaper 只在条目标了 in_flight_since 时才判终态。"""
    it.in_flight_since = time.time() - 1
    (art / "run.lock").write_text(str(_dead_pid()), encoding="utf-8")


@pytest.fixture
def reap_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, art = _in_flight_state(tmp_path, repo="a/b")
    posted: list[str] = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    monkeypatch.setattr("issue_keeper.keeper._channel_reply_posted",
                        lambda *a, **kw: False)
    return state, it, art, posted, tmp_path


def test_reaper_retry_later写退避与连击(reap_env):
    """retry-later 重派必须落一个退避截止 + 连击计数，否则同轮立刻再派。"""
    state, it, art, posted, home = reap_env
    _retry_later_row(home)

    _reap_pipelines(_pipeline_cfg(home / "b", poll_interval_secs=PROD_POLL),
                    state, _bindings(repo="a/b"))

    assert it.in_flight_since is None, "retry-later 要清在途锚（不消费首响）"
    assert it.retry_after is not None and it.retry_after > time.time(), (
        "retry-later 必须写退避截止（复用 it.retry_after），否则同轮立刻重派")
    assert getattr(it, "retry_later_streak", None) == 1, (
        "state 必须记 retry_later_streak=1（指数退避的连击基数）")
    assert posted == [], "retry-later 重派不补发认领评论"


def test_三连击间隔单调递增并封顶6h(reap_env):
    """7min → 1h → 6h（封顶），第 4 连仍 6h；间隔单调递增且不封顶失守。"""
    state, it, art, posted, home = reap_env
    cfg = _pipeline_cfg(home / "b", poll_interval_secs=PROD_POLL)
    delays: list[float] = []

    for _ in range(4):
        _arm_in_flight(it, art)
        _retry_later_row(home)
        t0 = time.time()
        _reap_pipelines(cfg, state, _bindings(repo="a/b"))
        assert it.retry_after, "每轮 retry-later 都要写退避截止"
        delays.append(it.retry_after - t0)

    assert delays[0] == pytest.approx(PROD_POLL, abs=5), (
        f"首连退避 = poll_interval_secs（7min），实测 {delays[0]:.0f}s")
    assert delays[1] == pytest.approx(BACKOFF_1H, abs=5), (
        f"二连退避 = 1h，实测 {delays[1]:.0f}s")
    assert delays[2] == pytest.approx(BACKOFF_CAP, abs=5), (
        f"三连退避 = 6h 上限，实测 {delays[2]:.0f}s")
    assert delays[3] == pytest.approx(BACKOFF_CAP, abs=5), (
        f"封顶后不再增长，实测 {delays[3]:.0f}s")
    assert delays[0] < delays[1] < delays[2], "间隔必须单调递增"
    # 封顶比较用 approx：每轮 t0 取在 reap 之前，纳秒级测量偏差必然存在
    assert delays[2] == pytest.approx(delays[3], abs=5), "封顶后不再增长"
    assert getattr(it, "retry_later_streak", None) == 4, "连击计数逐轮累加"


def test_退避未到点不派发_到点才派发(reap_env, monkeypatch):
    """退避闸必须在派发路径生效：未到期本轮 0 派发（也就 0 认领评论）。"""
    state, it, art, posted, home = reap_env
    _retry_later_row(home)
    _reap_pipelines(_pipeline_cfg(home / "b", poll_interval_secs=PROD_POLL),
                    state, _bindings(repo="a/b"))

    dispatched: list = []
    monkeypatch.setattr("issue_keeper.keeper._dispatch_pipeline",
                        lambda *a, **kw: dispatched.append(kw) or {"status": "dispatched"})
    env = _defer_env(_pipeline_on_cfg(), rs=state.repo("a-b"))

    assert _process_resource(**env) == 0
    assert dispatched == [], "retry-later 退避未到点不得派发（不再同轮重派）"

    it.retry_after = time.time() - 1        # 到点
    assert _process_resource(**env) == 1
    assert len(dispatched) == 1, "到点后正常重派"


def test_连击重派不发认领评论(tmp_path, monkeypatch):
    """空转重派（streak > 0）不贴认领评论；只有首次派发才贴。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text("pass\n", encoding="utf-8")
    _art(tmp_path)
    posted: list[str] = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    cfg = _pipeline_cfg(bridge, pipeline_claim_comment=True)

    it = ItemState()
    it.retry_later_streak = 2               # 本轮是 retry-later 空转重派
    _dispatch_pipeline(cfg, _bindings()[0], _res(number=11), it, "a/b issue#11")

    assert posted == [], (
        "retry-later 重派是空转，不得再贴「已认领开始处理」——"
        "同一 issue 连续空转期间评论数不得随轮次线性增长")

    posted.clear()
    _dispatch_pipeline(cfg, _bindings()[0], _res(number=12), ItemState(), "a/b issue#12")
    assert len(posted) == 1 and "已认领" in posted[0], "首次派发仍要贴认领评论"


def test_终态收尾清零连击与退避(reap_env):
    """run 真跑成功（非 retry-later 终态）后，连击与退避都清零——
    下一次真实环境闸失败重新从 7min 起算，不被历史连击顶到 6h。"""
    state, it, art, posted, home = reap_env
    _retry_later_row(home)
    _reap_pipelines(_pipeline_cfg(home / "b", poll_interval_secs=PROD_POLL),
                    state, _bindings(repo="a/b"))
    assert getattr(it, "retry_later_streak", None) == 1

    _arm_in_flight(it, art)
    _retry_later_row(home, status="done", error="")
    _reap_pipelines(_pipeline_cfg(home / "b", poll_interval_secs=PROD_POLL),
                    state, _bindings(repo="a/b"))

    assert it.processed is True, "成功终态收尾"
    assert getattr(it, "retry_later_streak", 0) == 0, "终态必须清零连击"
    assert it.retry_after is None, "终态不留幽灵退避（#8 既有语义）"


def test_streak随state持久化(tmp_path):
    """state.py 四处（字段/load/dump/合并白名单）漏一处即被静默丢弃——#9 的教训。"""
    path = tmp_path / "state.json"
    seed = load_state(path)
    it = seed.repo("a-b").item("7")
    it.retry_later_streak = 3
    it.retry_after = 500.0
    save_state(path, seed)

    back = load_state(path).repo("a-b").item("7")
    assert getattr(back, "retry_later_streak", None) == 3, (
        "retry_later_streak 未随 state 持久化（daemon 重启即丢连击 → 退避失效）")
    assert back.retry_after == 500.0


def test_日限豁免语义保留(tmp_path, monkeypatch):
    """验收回归哨兵：retry-later 仍不计 issue 日限 / 作者日限（守卫风暴 #87-#103）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    for _ in range(12):
        _ledger_row(tmp_path, "a/b", 9, status="retry-later", author="bob")

    cfg = Config(pipeline_issue_daily_limit=2, author_daily_limit=1)
    assert _issue_over_pipeline_limit(cfg, "a/b", 9) is False
    assert _author_over_limit(cfg, "bob") is False


def test_reopen清零连击():
    """人工 reopen 即重算连击：退避从 7min 起，不被历史连击顶到 6h。"""
    it = ItemState(processed=True, retry_after=time.time() + BACKOFF_CAP,
                   retry_later_streak=3)
    _clear_terminal_state(it)

    assert it.retry_later_streak == 0
    assert it.retry_after is None


def test_派发成功不清连击(tmp_path, monkeypatch):
    """设计决定钉死：派发只清 retry_after，**绝不**清 streak——
    清了每轮都从 7min 重来（指数退避等于没修），空转重派还会重新贴认领评论。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text("pass\n", encoding="utf-8")
    _art(tmp_path)
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment", lambda *a, **kw: None)
    cfg = _pipeline_cfg(bridge, pipeline_claim_comment=False)

    it = ItemState(processed=False)
    it.retry_later_streak = 1
    it.retry_after = time.time() + 60
    _dispatch_pipeline(cfg, _bindings()[0], _res(number=13), it, "a/b issue#13")

    assert it.retry_later_streak == 1, "派发成功不得清零连击"
    assert it.retry_after is None, "本次派发已消费退避，清掉免得残留"
