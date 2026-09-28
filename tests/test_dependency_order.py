"""批次依赖编排地板——拓扑排序 + 依赖唤醒（2026-09-28）。

背景：keeper 按 gh 列表序逐个派管线，不看批次内依赖关系——依赖者先跑会基于
未合并的依赖开工（09-27 批次 #28/#29 冲突），blocked 后依赖就绪也无人唤醒
（#17 零活动 33h 的机制性根因）。
"""
from types import SimpleNamespace

from issue_keeper.keeper import (
    _dependency_first_order,
    _extract_issue_refs,
    _wake_resolved_dependencies,
)
from issue_keeper.state import RepoState, State


def _res(num, body, processed=False):
    return SimpleNamespace(
        number=num, kind="issue", resource_key=str(num), body=body,
        title=f"issue {num}", author="a", state="open",
    )


def _state_with(*nums, processed=True):
    rs = RepoState()
    for n in nums:
        rs.item(str(n)).processed = processed
    return rs


# ── _extract_issue_refs ──────────────────────────────────────────────

def test_extract_refs_basic():
    assert _extract_issue_refs("依赖 #12，另见 #7 和 #12") == [12, 7]


def test_extract_refs_empty():
    assert _extract_issue_refs("no refs here") == []
    assert _extract_issue_refs("") == []
    assert _extract_issue_refs(None) == []


# ── _dependency_first_order ──────────────────────────────────────────

def test_order_dependency_first():
    # 列表序 #3 在前，但 #3 依赖 #2 → #2 先跑
    rs = _state_with()
    out = _dependency_first_order([_res(3, "依赖 #2"), _res(2, "独立")], rs)
    assert [r.number for r in out] == [2, 3]


def test_order_chain():
    rs = _state_with()
    out = _dependency_first_order(
        [_res(3, "见 #2 #1"), _res(2, "见 #1"), _res(1, "独立")], rs)
    assert [r.number for r in out] == [1, 2, 3]


def test_order_cycle_falls_back_to_list_order():
    rs = _state_with()
    out = _dependency_first_order([_res(1, "见 #2"), _res(2, "见 #1")], rs)
    assert [r.number for r in out] == [1, 2]


def test_order_ignores_refs_outside_batch():
    rs = _state_with()
    out = _dependency_first_order([_res(5, "依赖 #99（已关闭，不在批内）")], rs)
    assert [r.number for r in out] == [5]


def test_order_processed_first_then_pending_topo():
    rs = _state_with(9)  # #9 已处理
    out = _dependency_first_order(
        [_res(9, "旧 issue"), _res(3, "依赖 #2"), _res(2, "独立")], rs)
    assert [r.number for r in out] == [9, 2, 3]


# ── _wake_resolved_dependencies ──────────────────────────────────────

def _fake_src(open_numbers, prs=False):
    kinds_map = {"issue": [{"number": n} for n in open_numbers],
                 "pr": [{"number": n} for n in (prs or [])]}
    return SimpleNamespace(
        list_open=lambda repo, kinds, labels=None: [
            SimpleNamespace(number=e["number"], kind=k, resource_key=str(e["number"]))
            for k in kinds for e in kinds_map.get(k, [])
        ])


def _watch(rs, num, deps, processed=True):
    it = rs.item(str(num))
    it.processed = processed
    it.wakeup_deps = list(deps)


def test_wake_when_dep_closed(tmp_path):
    rs = RepoState()
    _watch(rs, 10, [5])
    src = _fake_src(open_numbers={10})  # #5 已关，#10 还开着
    _wake_resolved_dependencies(src, SimpleNamespace(repo="o/r", cwd=str(tmp_path),
                                                     monitor_prs=False), rs)
    it = rs.item("10")
    assert it.wakeup_deps == [] and it.processed is False


def test_no_wake_while_dep_open(tmp_path):
    rs = RepoState()
    _watch(rs, 10, [5])
    src = _fake_src(open_numbers={10, 5})  # #5 还开着
    _wake_resolved_dependencies(src, SimpleNamespace(repo="o/r", cwd=str(tmp_path),
                                                     monitor_prs=False), rs)
    it = rs.item("10")
    assert it.wakeup_deps == [5] and it.processed is True


def test_wake_when_fix_merged_to_main(tmp_path):
    # 依赖 issue 还开着，但修复 commit 已进 origin/main → 唤醒
    import os
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty", "-q",
                    "-m", "fix(cli): something (#5)"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_path), "update-ref",
                    "refs/remotes/origin/main", "HEAD"], check=True)
    rs = RepoState()
    _watch(rs, 10, [5])
    src = _fake_src(open_numbers={10, 5})
    _wake_resolved_dependencies(src, SimpleNamespace(repo="o/r", cwd=str(tmp_path),
                                                     monitor_prs=False), rs)
    it = rs.item("10")
    assert it.wakeup_deps == [] and it.processed is False


def test_partial_deps_stay_watched(tmp_path):
    rs = RepoState()
    _watch(rs, 10, [5, 6])
    src = _fake_src(open_numbers={10, 6})  # #5 闭合，#6 未闭合
    _wake_resolved_dependencies(src, SimpleNamespace(repo="o/r", cwd=str(tmp_path),
                                                     monitor_prs=False), rs)
    it = rs.item("10")
    assert it.wakeup_deps == [5, 6] and it.processed is True
