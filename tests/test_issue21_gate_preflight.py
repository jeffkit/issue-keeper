"""#21：门命令头预检——执行环境缺工具时，跑任何门之前 fail-fast。

实证（2026-10-08）：plaita 的 lint 门模板写 `uvx ruff check …`，AGS 沙箱镜像里
没有 uvx（宿主有、沙箱没有——门在沙箱里跑，宿主侧的检查拦不住）→ 每单都跑到
g1 才 `exit=127: uvx: command not found`，再被当"代码失败"烧掉一轮 20-40min 的
fix-loop。工具装没装是执行环境契约，与本次改动无关，必须在**执行环境**里一次
探清、立刻失败并标明「环境问题」。

测试用自造的缺失命令名（`uvx` 在本机存在，直接拿它当"缺工具"样本会看环境漂移）。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "flows" / "gates" / "gate_runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("gate_runner_issue21", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
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


def test_missing_head_fails_before_any_gate(repo, tmp_path):
    """g2 的命令头不存在 → 预检失败，g1 一门都不跑（不是"跑到 g1 才炸"）。"""
    spec = _spec(tmp_path, [
        {"name": "g1", "command": "touch ran-g1"},
        {"name": "lint", "command": "missing-tool-issue21 check . --select E722"},
    ])
    r = _run(spec, repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "PRECHECK FAIL" in r.stdout
    assert "missing-tool-issue21" in r.stdout and "lint" in r.stdout
    assert "PASS" not in r.stdout and "FAIL lint" not in r.stdout
    assert not (repo / "ran-g1").exists()


def test_available_heads_pass_preflight(repo, tmp_path):
    """命令头都在场 → 预检放行，门照常跑。"""
    spec = _spec(tmp_path, [
        {"name": "a", "command": "cd . && FOO=1 true"},
        {"name": "b", "command": f"{sys.executable} -c 'pass'"},
    ])
    r = _run(spec, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL GATES PASSED" in r.stdout and "PRECHECK" not in r.stdout


def test_path_skipped_gate_is_not_preflighted(repo, tmp_path):
    """paths 未触及的条件门不探（其工具不必在场）——否则无关改动会被它拦死。"""
    spec = _spec(tmp_path, [
        {"name": "tui-mutants", "command": "missing-tool-mutmut run",
         "paths": ["crates/recursive-tui/**"]},
    ])
    r = _run(spec, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SKIP tui-mutants" in r.stdout and "PRECHECK" not in r.stdout


def test_preflight_reports_all_missing_heads(repo, tmp_path):
    """一次报全（不是撞一个修一个）：两道门各缺一个工具都列出。"""
    spec = _spec(tmp_path, [
        {"name": "lint", "command": "missing-tool-a ruff check ."},
        {"name": "types", "command": "missing-tool-b ."},
    ])
    r = _run(spec, repo)
    assert r.returncode == 1
    assert "missing-tool-a" in r.stdout and "missing-tool-b" in r.stdout
    assert r.stdout.count("PRECHECK FAIL") == 1


def test_autofix_head_is_not_preflighted(repo, tmp_path):
    """autofix 只在门失败后跑，探它会凭空制造预检失败。"""
    spec = _spec(tmp_path, [{"name": "marker", "command": "test -f nope.marker",
                             "autofix": "no-such-autofix-tool"}])
    r = _run(spec, repo)
    assert r.returncode == 1
    assert "PRECHECK" not in r.stdout  # 预检放行；失败来自门本身 + autofix 自身失败
    assert "FAIL marker" in r.stdout and "autofix 自身失败" in r.stdout


def test_precheck_reports_reason_per_head(repo, tmp_path):
    """报告要分清「找不到」与「存在但没 exec 位」——后者改权限即可，不是缺工具。"""
    (repo / "check.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    spec = _spec(tmp_path, [
        {"name": "nope", "command": "./check.sh"},
        {"name": "gone", "command": "missing-tool-issue21 ."},
    ])
    r = _run(spec, repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "存在但没有执行权限 `./check.sh`" in r.stdout
    assert "找不到 `missing-tool-issue21`" in r.stdout


def test_command_heads_parsing():
    mod = _load_runner()
    assert mod.command_heads("uvx ruff check plaita tests --select E722") == ["uvx"]
    assert mod.command_heads("cd . && FOO=1 cargo test") == ["cargo"]
    assert mod.command_heads("echo a; exit 3") == []
    assert mod.command_heads("true") == []
    assert mod.command_heads("") == []
    assert mod.command_heads("FOO=1 BAR=2 make test") == ["make"]
    assert mod.command_heads("pnpm type-check && pnpm test:run") == ["pnpm", "pnpm"]
    assert mod.command_heads("bash .dev/scripts/tui-mutants.sh") == ["bash"]


def test_quoted_separators_are_not_segment_boundaries():
    """引号/替换里的 `|` `;` `&&` 是普通字符——按它们切段会编出不存在的 head，
    让工具齐备的门在预检阶段整体失败（#21 复核的假 PRECHECK FAIL）。"""
    mod = _load_runner()
    assert mod.command_heads('grep -qE "fix|feat" AGENTS.md') == ["grep"]
    assert mod.command_heads("python3 -c 'import p; p.check()'") == ["python3"]
    assert mod.command_heads('bash -c \'cd . && python3 -c "print(1); print(2)"\'') == ["bash"]
    assert mod.command_heads("""bash -c 'for f in a b; do echo $f; done'""") == ["bash"]
    assert mod.command_heads("echo 'a;b'") == []  # echo 是内建，不算可执行头
    # 解析不了的段跳过，绝不从残片里编 head；引号不配则整条放弃
    assert mod.command_heads("cargo test 'unbalanced") == []
    assert mod.command_heads("FOO=$(git rev-parse a && git rev-parse b) cargo test") == []
    assert mod.command_heads("cargo test `git rev-parse a`") == []  # 反引号段不可信：整段跳过


def test_quoted_separator_gate_is_not_a_false_precheck_fail(repo, tmp_path):
    """端到端：#21 复核里的两条命令，bash 跑得通就必须跑得通（不是 exit 1 零门）。"""
    (repo / "AGENTS.md").write_text("fix: something\n", encoding="utf-8")
    spec = _spec(tmp_path, [
        {"name": "lint", "command": 'grep -qE "fix|feat" AGENTS.md'},
        {"name": "unit", "command": """bash -c 'python3 -c "print(1); print(2)"'"""},
    ])
    r = _run(spec, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PRECHECK" not in r.stdout
    assert "PASS lint" in r.stdout and "PASS unit" in r.stdout
    assert "ALL GATES PASSED" in r.stdout


def test_head_problem_relative_path(repo, tmp_path):
    mod = _load_runner()
    script = repo / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    assert mod.head_problem("./check.sh", str(repo)) == "存在但没有执行权限"  # 还没 exec 位
    script.chmod(0o755)
    assert mod.head_problem("./check.sh", str(repo)) == ""
    assert mod.head_problem("./no-such.sh", str(repo)) == "找不到"
    assert mod.head_problem("definitely-no-such-tool-issue21", str(repo)) == "找不到"
