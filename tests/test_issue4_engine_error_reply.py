"""issue #4：引擎异常兜底回评的可诊断性。

来自 recursive#67（issue-keeper#4 复刻）：fix_loop→GATE 崩溃后兜底回评
1) err 只保留头部 140 字符——多层 NodeExecutionError 链的最内层
   FileNotFoundError 路径恰好被切掉；
2) 回评不含 gate 名/命令/cwd/目录存在性，崩溃无法自查。
"""

import json
import subprocess
import sys
import time

import pytest

from issue_keeper.config import Config, RepoBinding
from issue_keeper.keeper import _reap_pipelines
from tests.test_pipeline_dispatch_guard import (
    _bindings,
    _in_flight_state,
    _init_git_wt,
    _pipeline_cfg,
    _write_issue_ledger,
)


def _build_nested_error(total: int = 600) -> str:
    """构造 >320 字符的多层 NodeExecutionError 链，最内层是带文件名的 FileNotFoundError。"""
    inner = "FileNotFoundError: [Errno 2] No such file or directory: '/Users/x/.cargo/bin/cargo'"
    wrap = inner
    for name in ("run", "fix_loop", "g1"):
        wrap = f"NodeExecutionError: 执行节点{name}出错了: {wrap}"
    # 头部 padding 到足够长，确保任何「头部截断」都切掉最内层
    pad = "x" * max(0, total - len(wrap))
    return pad + wrap


@pytest.fixture
def reap_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, art = _in_flight_state(tmp_path, repo="a/b")
    posted = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    monkeypatch.setattr("issue_keeper.keeper._channel_reply_posted",
                        lambda *a, **kw: False)
    return state, it, art, posted


def test_engine_error_reply_keeps_innermost_exception_tail(reap_env, tmp_path):
    """尾部截断/最内层给足长度：>320 字符异常链的 FileNotFoundError 路径必须完整可见。"""
    state, it, art, posted = reap_env
    err = _build_nested_error()
    assert len(err) > 320
    _write_issue_ledger(tmp_path, "a/b", 7,
                        {"status": "engine_error", "comment_posted": False, "error": err})

    _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b"))

    assert len(posted) == 1
    body = posted[0]
    assert "No such file or directory" in body          # 最内层异常类型可见
    assert "/Users/x/.cargo/bin/cargo" in body          # 文件名/路径不被切掉


def test_engine_error_reply_includes_gate_context(reap_env, tmp_path):
    """崩溃回评附带上下文字段：gate 名 / 命令 / cwd / 目录是否存在。"""
    state, it, art, posted = reap_env
    err = _build_nested_error()
    _write_issue_ledger(tmp_path, "a/b", 7, {
        "status": "engine_error", "comment_posted": False, "error": err,
        "gate_failed": "repo-tests",
    })
    # dispatch.json 是 gate 命令与 cwd 的事实源（_dispatch_pipeline 落盘）
    (art / "dispatch.json").write_text(json.dumps({
        "test_command": "python3 -m pytest tests/ -q",
        "worktree_dir": str(tmp_path / "clone" / ".worktrees" / "issue-7"),
    }), encoding="utf-8")

    _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state, _bindings(repo="a/b"))

    body = posted[0]
    assert "repo-tests" in body
    assert "pytest tests/" in body                      # 命令
    assert ".worktrees/issue-7" in body                 # cwd
    assert ("不存在" in body) or ("worktree 目录" in body)  # 目录存在性有明示


def test_engine_error_checks_worktree_and_logs(reap_env, tmp_path, monkeypatch, caplog):
    """engine_error 路径触发 worktree 状态检查（已有 wip 快照）且留有日志记录。

    #4 验收第 3 条：检查/清理动作本身要落日志（现快照只有 note 非空才 log.warning，
    目录不存在这条路径完全静默）。
    """
    import logging
    state, it, art, posted = reap_env
    repo_dir = tmp_path / "clone"
    wt = repo_dir / ".worktrees" / "issue-7"
    g = _init_git_wt(wt)
    (wt / "half.txt").write_text("半成品", encoding="utf-8")
    _write_issue_ledger(tmp_path, "a/b", 7,
                        {"status": "engine_error", "comment_posted": False, "error": "boom"})

    with caplog.at_level(logging.WARNING, logger="issue-keeper"):
        _reap_pipelines(_pipeline_cfg(tmp_path / "b"), state,
                        [RepoBinding(repo="a/b", profile="p", cwd=str(repo_dir))])

    assert "wip(issue-7)" in g("log", "-1", "--format=%s").stdout     # 检查确实发生
    assert any("worktree" in r.message for r in caplog.records), caplog.text
