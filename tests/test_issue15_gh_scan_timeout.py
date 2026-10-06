"""#15 回归测试——gh 扫描调用没有 timeout 时，一次卡顿会停摆整轮。

锚点：`issue_keeper/sources/github.py:112-114`

    def _run_gh(args: list[str]) -> str:
        cmd = ["gh", *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)   # ← 无 timeout

扫描路径（`GitHubSource.list_open` → `_list_one` → `_run_gh`，代码里没有一处
传 timeout，见同文件 :275 的 urllib `timeout=30`、`keeper.py:1310/1322` 的
`_gh_post_comment` / `_gh_add_label` `timeout=60`）——`gh` 卡住时 `subprocess.run`
永不返回：进程 alive、CPU 0、日志停在「扫描仓库 a/b ...」、`run_once` 轮尾的
`save_state_merged`（`keeper.py:832`）永不执行、`run_daemon`（`keeper.py:1040-1048`）
的 `while True` 进不去下一轮，也没有任何看门狗能救（KeepAlive 只在进程死时重启）。

修复形状（本文件即验收）：
  1. `_run_gh` 把 `timeout=_GH_TIMEOUT_SECS`（模块常量，≤60s）传给 `subprocess.run`；
  2. `subprocess.TimeoutExpired` 在 `_run_gh` 内转成 `RuntimeError`（带 "超时" 文案）
     —— 必须转，因为 `_fetch_review_comments_gh`(:124) 与 `GitHubSource.self_identity`(:205)
     只捕 `RuntimeError`，而 `process_repo` 的 `me = src.self_identity()`(keeper.py:471)
     **不在任何 try 里**，TimeoutExpired 从那里冒出去会掀掉整轮（只有 daemon 层
     `except Exception` 兜住 → raise 型缺陷，不是「该仓本轮失败」）；
  3. 落到 `keeper.py:487-491` 的既有 per-kind `except Exception` → `log.error("列出 %s 失败")`
     + `continue`，该仓本轮跳过、下一轮自然重试（state 未置 processed）。

修复前：本文件全红（其中超时用例会挂到 wrapper 自己退出为止）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from issue_keeper import keeper
from issue_keeper.config import Config, RepoBinding
from issue_keeper.sources import github as gh_mod
from issue_keeper.sources.github import GitHubSource, _run_gh
from issue_keeper.state import State

_LIST_ROW = (
    '[{"number": 7, "title": "t", "body": "b", "state": "open", "labels": [],'
    ' "author": {"login": "bob"}, "createdAt": "2026-10-01T00:00:00Z",'
    ' "updatedAt": "2026-10-01T00:00:00Z"}]'
)


def _recording_run(calls: list):
    def run(cmd, **kwargs):
        calls.append((list(cmd), dict(kwargs)))
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
    return run


def test_wip_run_gh_passes_bounded_timeout(monkeypatch):
    """扫描路径的 gh 调用必须传有限 timeout（≤60s，与收尾路径对齐）。"""
    calls: list = []
    monkeypatch.setattr(gh_mod.subprocess, "run", _recording_run(calls))
    _run_gh(["issue", "list", "--repo", "a/b"])

    (cmd, kwargs), = calls
    timeout = kwargs.get("timeout")
    assert timeout is not None, "gh 扫描调用没有 timeout：网络卡顿即整轮停摆"
    assert 0 < timeout <= 60, f"timeout={timeout} 超出 60s 预算"


def test_wip_timeout_expired_converted_to_runtime_error(monkeypatch):
    """`subprocess.TimeoutExpired` 必须在 `_run_gh` 内转成 RuntimeError。

    否则它从 `src.self_identity()`（keeper.py:471，唯一的 `except RuntimeError`）
    与 `_fetch_review_comments_gh`(:124) 处逃逸，掀掉整轮而非「该仓本轮失败」。
    """
    def _hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 60)

    monkeypatch.setattr(gh_mod.subprocess, "run", _hang)

    with pytest.raises(RuntimeError) as ei:
        _run_gh(["issue", "list", "--repo", "a/b"])

    assert not isinstance(ei.value, subprocess.TimeoutExpired)
    msg = str(ei.value)
    assert "超时" in msg and "gh issue list" in msg


def test_wip_hanging_gh_does_not_block_scan(tmp_path, monkeypatch):
    """PATH 前置 sleep-then-fail 的 gh wrapper：扫描必须按时失败返回，不许挂住。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "gh"
    wrapper.write_text("#!/bin/sh\nsleep 5\nexit 1\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    # 生产值 60s；测试里压到 1s 才能在秒级观察到超时路径
    monkeypatch.setattr(gh_mod, "_GH_TIMEOUT_SECS", 1)

    started = time.monotonic()
    with pytest.raises(RuntimeError) as ei:
        GitHubSource().list_open("a/b", ["issue"])
    elapsed = time.monotonic() - started

    assert elapsed < 3, f"扫描被卡住的 gh 拖了 {elapsed:.1f}s"
    assert "超时" in str(ei.value)


def test_wip_transient_timeout_does_not_stick_to_next_round(monkeypatch):
    """超时只影响本轮：下一次调用照常拿到数据（无粘滞状态）。"""
    calls = {"n": 0}

    def _flaky(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 60)
        return subprocess.CompletedProcess(cmd, 0, stdout=_LIST_ROW, stderr="")

    monkeypatch.setattr(gh_mod.subprocess, "run", _flaky)

    with pytest.raises(RuntimeError):
        GitHubSource().list_open("a/b", ["issue"])
    assert [r.number for r in GitHubSource().list_open("a/b", ["issue"])] == [7]


def test_wip_repo_scan_timeout_is_logged_as_repo_failure(tmp_path, monkeypatch, caplog):
    """超时 = 「该仓本轮扫描失败」告警，不是未捕获异常掀整轮。

    `process_repo` 在 `me = src.self_identity()`(keeper.py:471) 之前/之后都没有整体
    try，`self_identity()` 里只捕 RuntimeError——转成 RuntimeError 后，错误落到
    keeper.py:487-491 的 per-kind except，记日志、continue、本轮照常收尾。
    """
    def _hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 60)

    monkeypatch.setattr(gh_mod.subprocess, "run", _hang)
    monkeypatch.setattr(keeper, "_ensure_profile",
                        lambda binding, cache: SimpleNamespace(name="fake", is_hub=False,
                                                               cwd=None, env={}))

    binding = RepoBinding(repo="a/b", profile="p")
    cfg = Config(repos=[binding], state_file=tmp_path / "state.json", pipeline_mode=False)

    with caplog.at_level(logging.DEBUG, logger="issue-keeper"):
        handled = keeper.process_repo(binding, cfg, State(), {}, {})

    assert handled == 0
    assert any("列出 issue 失败" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


def test_wip_writeback_gh_calls_keep_timeout_60(monkeypatch):
    """回归护栏：收尾路径的既有 timeout=60 不被本次改动动到。"""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _recording_run(calls))

    keeper._gh_post_comment("issue", "a/b", 5, "body")
    keeper._gh_add_label("issue", "a/b", 5, "needs-human")

    assert [kwargs.get("timeout") for _, kwargs in calls] == [60, 60]
