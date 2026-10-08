"""#11 回归——guard 的 `.github/` 禁改名单需要显式人工通道（ilink-hub #39 实证）。

实证场景：CI 修复单（如给 workflows 加 `RUST_TEST_THREADS=1`）实现完成、
测试全绿，仅因目标文件 `.github/workflows/ci.yml` 在禁改名单里就以
guarded 收尾——只能值守手工落地，绕过管线的三门评审（流程不对称）。

修复形状（双闸门，缺一仍拦）：
  1. 仓库契约 `allow_github_paths`（config.py）声明允许的 `.github` 子路径
     前缀——声明权在契约；
  2. issue 正文头部 `ci-fix: true`（keeper._declares_ci_fix）显式 opt-in——
     触发权在 issue。keeper 把两者合成进 dispatch payload 的
     `allow_github_paths`（空 = 无例外）；
  3. flow guard（issue_pipeline_flow.py，钉在编译产物 flow.json）只对清单
     前缀内的 `.github/**` 放行：`.worktrees/`、绝对路径、目录穿越、清单外
     的 `.github/**` 一律照旧拦截。

本文件只驱动 guard 节点本身（真 code 节点跑在临时 git worktree 上），
不走整条 flow——路由语义（guarded 早退）已由既有测试网覆盖。
"""
import json
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
FLOW_JSON = HERE.parent / "flows" / "issue-pipeline.flow.json"

sys.path.insert(0, str(HERE.parent / "flows"))


def _run_guard(wt: pathlib.Path, allow) -> dict:
    """从生产 flow 定义取 guard 节点并直接执行（真 code 节点）。"""
    from plaita.core.executor import FlowExecution
    from plaita.node import get_default_registry, register_code_node

    register_code_node(default_backend="subprocess")

    ir = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
    spec = next(n for n in ir["nodes"] if n.get("id") == "guard")

    reg = get_default_registry()
    node = reg.get(spec["type"]).model_validate({**spec, "next": None})
    execution = FlowExecution()

    class _Flow:
        expose_env = []
        global_context = {}
        flow_id = "issue-pipeline"

    execution.setup_flow(_Flow, (), {"worktree_dir": str(wt),
                                     "allow_github_paths": allow})
    return node.execute(execution)


def _init_git_wt(path: pathlib.Path):
    path.mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", "-C", str(path), *a],
                                  capture_output=True, text=True)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (path / "base.txt").write_text("base", encoding="utf-8")
    g("add", "-A")
    g("commit", "-qm", "base")
    return g


def _dirty(g, wt: pathlib.Path, rel: str, text="x"):
    p = wt / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    g("add", "-N", rel)  # intent-to-add：未跟踪文件也要进 git diff HEAD（生产语义）


def test_ci_workflow_fix_passes_with_allow_list(tmp_path):
    """正向（验收 1）：契约放行 .github/workflows/ + 声明 ci-fix → 改 workflows 过 guard。"""
    wt = tmp_path / "wt"
    g = _init_git_wt(wt)
    _dirty(g, wt, ".github/workflows/ci.yml", "RUST_TEST_THREADS: '1'\n")

    out = _run_guard(wt, [".github/workflows/"])
    assert out["ok"] is True
    assert out["violations"] == []
    assert out["github_allowed"] is True


def test_undeclared_github_change_still_guarded(tmp_path):
    """负向（验收 2）：未声明的 .github 修改照旧 guarded——例外通道没开。"""
    wt = tmp_path / "wt"
    g = _init_git_wt(wt)
    _dirty(g, wt, ".github/workflows/ci.yml")

    out = _run_guard(wt, [])          # keeper 侧未合成例外（无声明或无契约）
    assert out["ok"] is False
    assert out["violations"] == [".github/workflows/ci.yml"]
    assert out["github_allowed"] is False


@pytest.mark.parametrize("rel", [
    ".worktrees/other/leak.txt",          # 管线工作树自指
])
def test_non_github_redlines_unaffected_by_allow_list(tmp_path, rel):
    """allow 清单只放宽 .github 子路径——其余红线（.worktrees）
    在有例外时也必须照拦（防注入借道）。"""
    wt = tmp_path / "wt"
    g = _init_git_wt(wt)
    _dirty(g, wt, rel)

    out = _run_guard(wt, [".github/workflows/"])
    assert out["ok"] is False
    assert out["violations"] == [rel]


def test_traversal_and_absolute_paths_unreachable(tmp_path):
    """git 拒绝把 `..` / 绝对路径写进 index（rc=128）——这两类红线在真实
    worktree 上不可达，guard 的 `..`/startswith('/') 判据是纵深防御。
    这里钉死 git 的拒绝行为，防止上游 git 变化悄悄打开穿越通道。"""
    wt = tmp_path / "wt"
    g = _init_git_wt(wt)
    blob = subprocess.run(["git", "-C", str(wt), "hash-object", "-w", "--stdin"],
                          input="evil", capture_output=True, text=True).stdout.strip()
    for evil in ("/abs/evil.sh", ".github/workflows/../../evil.sh"):
        r = subprocess.run(["git", "-C", str(wt), "update-index", "--add",
                            "--cacheinfo", f"100644,{blob},{evil}"],
                           capture_output=True, text=True)
        assert r.returncode != 0, f"git 意外接受了非法路径 {evil}"


def test_allow_list_is_prefix_scoped(tmp_path):
    """前缀语义：放行 .github/workflows/ 不连带放行 .github/ 其余部分。"""
    wt = tmp_path / "wt"
    g = _init_git_wt(wt)
    _dirty(g, wt, ".github/workflows/deploy.yml")   # 清单内 → 过
    out = _run_guard(wt, [".github/workflows/"])
    assert out["ok"] is True

    wt2 = tmp_path / "wt2"
    g2 = _init_git_wt(wt2)
    _dirty(g2, wt2, ".github/dependabot.yml")       # 清单外 → 拦
    out2 = _run_guard(wt2, [".github/workflows/"])
    assert out2["ok"] is False
    assert out2["violations"] == [".github/dependabot.yml"]
