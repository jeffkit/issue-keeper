"""#7 回归：run 结束 → 轮首收尾（实测中位 533s）窗口内的人工 reopen 不得被吃掉。

复现形状（收尾只在 `run_once` 开头一次，`keeper.py` `_reap_pipelines`）：

    派发 T0（in_flight_since=T0）── run 跑完 ──[窗口，中位 533s]── 轮首收尾
                                                   ↑ 人工在这里 `reopen`
    旧行为：条目 processed=False 且 blocked=False → `reopen_issues` 判「无需改动」
            直接返回（CLI 打印「本就没被消费」）；随后 reaper 无条件
            `processed=True` → 人工的重跑意图静默蒸发。
    新行为：reopen 记 `manual_reopen_at`（在途条目也算改过 + 补状态评论）；
            reaper 收尾见它晚于本次派发 → 判 run 结论作废：清在途锚但**不置**
            processed → `run_once` 随后的扫仓当轮直接重派。
"""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime

from issue_keeper import console_exec as ce
from issue_keeper import keeper
from issue_keeper.config import RepoBinding
from issue_keeper.keeper import _clear_terminal_state, _reap_pipelines, reopen_issues, run_once
from issue_keeper.state import State, load_state, save_state, save_state_item, save_state_merged
from tests.test_console_exec import FakeClient, _cfg as _console_cfg
from tests.test_pipeline_dispatch_guard import (
    _bindings,
    _in_flight_state,
    _pipeline_cfg,
    _write_issue_ledger,
)


def _state_cfg(tmp_path, *, timeout: int = 30):
    binding = RepoBinding(repo="a/b", profile="p")
    return _pipeline_cfg(tmp_path / "b", pipeline_timeout_secs=timeout,
                         repos=[binding], state_file=tmp_path / "state.json")


def test_窗口内reopen不被processed吃掉(tmp_path, monkeypatch):
    """run 已结束、reaper 未收尾的窗口内 reopen → 收尾跳过 processed 并清在途锚。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _state_cfg(tmp_path)
    state, _it, _art = _in_flight_state(tmp_path, repo="a/b", since=time.time() - 5)
    save_state(cfg.state_path, state)
    _write_issue_ledger(tmp_path, "a/b", 7, {"status": "done", "comment_posted": False})
    posted: list[str] = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))

    assert reopen_issues(cfg, "a/b", [7]) == [7]

    # daemon 轮首收尾（与 run_once 同式：重读盘上状态）
    fresh = load_state(cfg.state_path)
    _reap_pipelines(cfg, fresh, _bindings(repo="a/b"))
    fit = fresh.repo("a-b").item("7")

    assert fit.processed is False, "窗口内 reopen 被 processed=True 静默吃掉（#7）"
    assert fit.in_flight_since is None, "要清在途锚，随后的扫仓才能重派"
    assert fit.manual_reopen_at is None, "reopen 标记消费后清零"
    assert posted == [], "结论作废的 run 不再补兜底回评"


def test_reopen在途条目也算改动并保留在途锚(tmp_path):
    """在途条目 processed/blocked 都为 False：旧逻辑判「无需改动」→ 人工 reopen 白做。"""
    cfg = _state_cfg(tmp_path)
    state, _it, _art = _in_flight_state(tmp_path, repo="a/b", since=time.time() - 5)
    save_state(cfg.state_path, state)

    assert reopen_issues(cfg, "a/b", [7]) == [7]

    back = load_state(cfg.state_path).repo("a-b").item("7")
    assert back.manual_reopen_at is not None, "在途 reopen 必须留下重跑意图的痕迹"
    assert back.in_flight_since is not None, "reopen 不得丢在途锚——reaper 还要认它"


def test_派发前的reopen不抑制收尾(tmp_path, monkeypatch):
    """陈旧标记（早于本次派发）不得让真跑完的 run 白跑。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, _art = _in_flight_state(tmp_path, repo="a/b", since=time.time())
    it.manual_reopen_at = time.time() - 60
    _write_issue_ledger(tmp_path, "a/b", 7, {"status": "done", "comment_posted": True})

    _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b"))

    assert it.processed is True
    assert it.manual_reopen_at is None


def test_运行中的reopen等run结束后再作废(tmp_path, monkeypatch):
    """run 还在跑时 reopen：收尾不动在途锚（等 run 结束），重跑意图留到那时才消费。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, _art = _in_flight_state(tmp_path, repo="a/b", live=True)
    it.manual_reopen_at = time.time()

    _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b"))

    assert it.in_flight_since is not None, "run 还在跑：不能提前清在途锚"
    assert it.manual_reopen_at is not None, "重跑意图要留到 run 结束才消费"


def test_run_once收尾后当轮重派(tmp_path, monkeypatch):
    """收尾把条目留成「未处理、非在途」——扫仓阶段同轮即可重派。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _state_cfg(tmp_path)
    state, _it, _art = _in_flight_state(tmp_path, repo="a/b", since=time.time() - 5)
    save_state(cfg.state_path, state)
    _write_issue_ledger(tmp_path, "a/b", 7, {"status": "done", "comment_posted": True})
    assert reopen_issues(cfg, "a/b", [7]) == [7]

    seen: dict = {}

    def _fake_process(binding, config, state, profile_cache, source_cache):
        item = state.repo("a-b").item("7")
        seen["processed"] = item.processed
        seen["in_flight"] = item.in_flight_since
        return 0

    monkeypatch.setattr(keeper, "process_repo", _fake_process)
    monkeypatch.setattr(keeper, "keeper_patrol", lambda *a, **k: 0)

    run_once(cfg)

    assert seen == {"processed": False, "in_flight": None}, (
        "收尾后必须是「未处理、非在途」，扫仓阶段才会重派（不消费首响）")


def test_manual_reopen_at随state持久化(tmp_path):
    """state.py 四处（字段/load/dump/合并白名单）漏一处即被静默丢弃——#9 的教训。"""
    path = tmp_path / "state.json"
    seed = load_state(path)
    seed.repo("a-b").item("7").manual_reopen_at = 1700000000.0
    save_state(path, seed)

    back = load_state(path).repo("a-b").item("7")
    assert back.manual_reopen_at == 1700000000.0


def test_轮尾合并写保留cli窗口内reopen标记(tmp_path):
    """条目轮首已带标记（P1 窗口）、轮内收尾消费了它、轮中途 CLI 又 reopen 一次：
    轮尾合并写不得把 CLI 的新标记盖回 None（#7×#9——否则 CLI 报「已重新入队」，
    条目却在下一次收尾被 processed=True 吃掉）。"""
    path = tmp_path / "state.json"
    seed = load_state(path)
    item = seed.repo("a-b").item("7")
    item.in_flight_since = 1000.0
    item.manual_reopen_at = 1500.0      # 轮首盘上已有一次窗口内 reopen
    save_state(path, seed)

    mem = load_state(path)          # daemon 轮首
    snapshot = copy.deepcopy(mem)   # 轮首快照（轮尾 diff 基线）

    mem.repo("a-b").item("7").manual_reopen_at = None    # 轮内收尾消费掉旧标记
    mem.repo("a-b").item("7").in_flight_since = 2000.0   # 同轮按陈旧标记重派

    save_state_item(path, "a-b", "7", _clear_terminal_state)   # 轮中途 CLI 再 reopen

    save_state_merged(path, mem, snapshot)

    back = load_state(path).repo("a-b").item("7")
    assert back.manual_reopen_at is not None, (
        "轮尾合并写把 CLI 的新 reopen 标记盖回 None（#7：重跑意图静默蒸发）")
    assert back.manual_reopen_at > 2000.0, "保留的必须是 CLI 的新标记，不是轮首旧值"


def test_合并写不反盖本轮收尾时的人工reopen(tmp_path):
    """daemon 本轮已收尾（mem: processed=True）+ CLI 中途 reopen 在途条目：
    合并写必须让盘上 CLI 的清终态优先——否则 CLI 报「已重新入队」却静默无效。"""
    path = tmp_path / "state.json"
    seed = load_state(path)
    seed.repo("a-b").item("7").in_flight_since = 1000.0
    save_state(path, seed)

    mem = load_state(path)
    snapshot = copy.deepcopy(mem)

    save_state_item(path, "a-b", "7", _clear_terminal_state)   # 轮中途 CLI reopen

    mem.repo("a-b").item("7").processed = True                 # 轮内 daemon 已收尾
    mem.repo("a-b").item("7").in_flight_since = None

    save_state_merged(path, mem, snapshot)

    back = load_state(path).repo("a-b").item("7")
    assert back.processed is False, "daemon 收尾把 CLI 已写盘的 reopen 反盖回去（#7）"
    assert back.in_flight_since is None, "收尾要清在途锚，下一轮扫仓才能重派"


def test_console窗口内reopen不出终态回评(tmp_path, monkeypatch):
    """engine=v2-console（F3）：窗口内 reopen 作废的 run 不得再发终态回评/落台账行。

    console 的收尾分流（`_reap_console_execution`）自己出回评 + 台账行，早于旧的
    void 判定——结论作废却已回评，日志声称不作数而 issue 页面上留了终态文案。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    art = tmp_path / ".issue-keeper" / "pipeline" / "b-7"
    art.mkdir(parents=True)
    (art / keeper.CONSOLE_EXEC_RECORD).write_text(
        json.dumps({"execution_id": "exec-123", "retry_count": 0}))
    done = {"status": "completed",
            "context": {"$NODE": {"verdict": {"verdict": "committed",
                                              "via": "git-publish"}}}}
    monkeypatch.setattr(ce, "client_from_config", lambda cfg: FakeClient(done))
    posted: list[str] = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    cfg = _console_cfg(state_file=tmp_path / "state.json")
    state = State()
    it = state.repo("a-b").item("7")
    it.in_flight_since = time.time() - 5
    it.manual_reopen_at = time.time()          # 窗口内人工 reopen

    _reap_pipelines(cfg, state, _bindings(repo="a/b"))

    assert posted == [], "结论作废的 console run 仍发了终态回评（F3）"
    assert not (art.parent / "runs.jsonl").exists(), "结论作废不该落台账行（F3）"
    assert it.in_flight_since is None, "清在途锚，随后扫仓当轮重派"
    assert it.manual_reopen_at is None and it.processed is False
    assert not (art / keeper.CONSOLE_EXEC_RECORD).exists(), "锚要清掉，重派才不被挡"
    assert keeper._pipeline_in_flight(art) is None


def test_console运行中的reopen不提前作废(tmp_path, monkeypatch):
    """console run 还在跑：窗口内 reopen 不得提前清锚（会双跑），等终态再作废。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    art = tmp_path / ".issue-keeper" / "pipeline" / "b-7"
    art.mkdir(parents=True)
    (art / keeper.CONSOLE_EXEC_RECORD).write_text(
        json.dumps({"execution_id": "exec-123", "retry_count": 0}))
    fresh = {"status": "running", "last_update_time": datetime.now().isoformat()}
    monkeypatch.setattr(ce, "client_from_config", lambda cfg: FakeClient(fresh))
    cfg = _console_cfg(state_file=tmp_path / "state.json")
    state = State()
    it = state.repo("a-b").item("7")
    it.in_flight_since = time.time() - 5
    it.manual_reopen_at = time.time()

    _reap_pipelines(cfg, state, _bindings(repo="a/b"))

    assert it.in_flight_since is not None, "run 还在跑：不能提前清锚（否则双跑）"
    assert it.manual_reopen_at is not None, "重跑意图要留到 run 结束才消费"
    assert (art / keeper.CONSOLE_EXEC_RECORD).exists()
