"""gate_runner 多门调度器测试：pass/fail/timeout/路径条件跳过/改动文件发现。

门语义来自 recursive 的真实需求：触及 recursive-tui 才跑 tui-mutants（且预算
40-60min）、fmt/clippy/test 恒跑。这里用临时 git 仓 + 轻量命令验证调度行为。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "flows" / "gates" / "gate_runner.py"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    """main 上有一个提交、再开 feature 分支的临时仓。"""
    wt = tmp_path / "wt"
    wt.mkdir()
    _git("init", "-b", "main", cwd=wt)
    _git("config", "user.email", "t@t", cwd=wt)
    _git("config", "user.name", "t", cwd=wt)
    (wt / "readme").write_text("v1")
    _git("add", "-A", cwd=wt)
    _git("commit", "-m", "init", cwd=wt)
    _git("checkout", "-b", "pipeline/issue-1", cwd=wt)
    return wt


def _spec(tmp_path, gates, base="main") -> str:
    p = tmp_path / "gates.json"
    p.write_text(json.dumps({"base": base, "gates": gates}), encoding="utf-8")
    return str(p)


def _run(spec: str, cwd: Path):
    return subprocess.run(
        [sys.executable, str(RUNNER), "--spec", spec, "--cwd", str(cwd)],
        capture_output=True, text=True, timeout=60,
    )


def test_all_gates_pass(repo, tmp_path):
    spec = _spec(tmp_path, [
        {"name": "a", "command": "true"},
        {"name": "b", "command": "exit 0"},
    ])
    r = _run(spec, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS a" in r.stdout and "PASS b" in r.stdout
    assert "ALL GATES PASSED" in r.stdout


def test_first_failure_stops_and_reports(repo, tmp_path):
    spec = _spec(tmp_path, [
        {"name": "bad", "command": "echo boom >&2; exit 3"},
        {"name": "never", "command": "true"},
    ])
    r = _run(spec, repo)
    assert r.returncode == 1
    assert "FAIL bad" in r.stdout and "boom" in r.stdout
    assert "PASS never" not in r.stdout  # 先修再跑：坏基线上不继续烧门


def test_timeout_kills_and_reports(repo, tmp_path):
    spec = _spec(tmp_path, [
        {"name": "slow", "command": "sleep 30", "timeout_secs": 1},
    ])
    r = _run(spec, repo)
    assert r.returncode == 1
    assert "TIMEOUT" in r.stdout or "timeout" in r.stdout


def test_path_condition_skips_untouched(repo, tmp_path):
    """触及无关文件时条件门跳过；触及指定路径才跑。"""
    spec = _spec(tmp_path, [
        {"name": "tui", "command": "echo ran", "paths": ["crates/recursive-tui/**"]},
    ])
    # 只改 docs 文件 → tui 门 SKIP
    (repo / "docs" / "x.md").parent.mkdir(exist_ok=True)
    (repo / "docs" / "x.md").write_text("doc")
    r = _run(spec, repo)
    assert r.returncode == 0
    assert "SKIP tui" in r.stdout and "ran" not in r.stdout

    # 触及 recursive-tui（untracked 也算）→ 门真跑
    tui = repo / "crates" / "recursive-tui" / "src" / "lib.rs"
    tui.parent.mkdir(parents=True)
    tui.write_text("pub fn x() {}")
    r = _run(spec, repo)
    assert r.returncode == 0
    assert "PASS tui" in r.stdout


def test_committed_changes_on_branch_count_as_touched(repo, tmp_path):
    """分支上已提交（vs origin/main 形态的三点 diff）也触发路径条件。"""
    # 无远端时 origin/main 不存在——三点 diff 空串，靠未提交侧命中即可；
    # 这里验证已提交改动通过 status/diff HEAD 侧不会丢（防御回归）。
    (repo / "crates" / "tui" / "a.rs").parent.mkdir(parents=True)
    (repo / "crates" / "tui" / "a.rs").write_text("x")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "tui change", cwd=repo)
    spec = _spec(tmp_path, [
        {"name": "tui", "command": "true", "paths": ["crates/tui/**"]},
    ])
    r = _run(spec, repo)
    assert r.returncode == 0
    assert "PASS tui" in r.stdout  # 已提交改动仍触发（不是只看未提交）


def test_empty_spec_fails_loud(repo, tmp_path):
    spec = _spec(tmp_path, [])
    r = _run(spec, repo)
    assert r.returncode == 1
    assert "门清单为空" in r.stdout


def test_repo_local_discovery(repo, tmp_path):
    """无 --spec 时找仓内 .issue-keeper/gates.json（惯例归仓）。"""
    local = repo / ".issue-keeper"
    local.mkdir()
    (local / "gates.json").write_text(
        json.dumps({"gates": [{"name": "local", "command": "true"}]}), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(repo)],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "PASS local" in r.stdout


def test_autofix_recovers_mechanical_gate(repo, tmp_path):
    """autofix：门失败→跑确定性修复→重检通过（#70 fmt 教训：别烧 fix-loop 预算）。"""
    spec = _spec(tmp_path, [{"name": "marker",
                             "command": "test -f done.marker",
                             "autofix": "touch done.marker"}])
    r = _run(spec, repo)
    assert r.returncode == 0
    assert "AUTO-FIX marker" in r.stdout
    assert "PASS marker" in r.stdout and "autofix 后重检通过" in r.stdout


def test_autofix_useless_keeps_gate_failed(repo, tmp_path):
    """autofix 自身失败 → 保留原失败（附 autofix 输出），不重检、退出码非零。"""
    spec = _spec(tmp_path, [{"name": "hopeless",
                             "command": "echo ran >> runs.log; exit 7",
                             "autofix": "echo nope >&2; exit 3"}])
    r = _run(spec, repo)
    assert r.returncode == 1
    assert "FAIL hopeless" in r.stdout
    assert "autofix 自身失败" in r.stdout and "原门失败" in r.stdout
    assert (repo / "runs.log").read_text().count("ran") == 1  # autofix 失败：原门只跑一次


def test_autofix_ok_but_gate_still_fails(repo, tmp_path):
    """autofix 成功但门仍不过 → 恰好重检一次后按原样失败（不死循环）。"""
    spec = _spec(tmp_path, [{"name": "hopeless",
                             "command": "echo ran >> runs.log; exit 7",
                             "autofix": "true"}])
    r = _run(spec, repo)
    assert r.returncode == 1
    assert "FAIL hopeless" in r.stdout
    assert (repo / "runs.log").read_text().count("ran") == 2  # 恰好重检一次，不死循环
