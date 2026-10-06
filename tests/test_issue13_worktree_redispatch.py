"""issue #13 [wip] 失败复现：worktree 已被回收/未注册时，重派首跳必炸。

现场证据（~/.issue-keeper/pipeline/agentproc-8|17）：

- ``nodes/wt_add.json`` → ``{"ok": false, "exit_code": 255,
  "stderr": "fatal: a branch named 'pipeline/issue-8' already exists"}``
- ``bridge-20261005-000620.log`` → ``RESULT {"status": "engine_error",
  "error": "执行节点setup出错了: RuntimeError: FileNotFoundError: ... .worktrees/issue-8 (源码第 208 行)"}``

机理：清树（worktree 目录被回收）但管线分支 ``pipeline/issue-N`` 留在仓里，
flow 的旧 ``wt_add`` 是 ``git worktree add -b <branch>``（:200-204）且其 CAPTURE
结果从未被引用 → 返回码 255 被静默吞掉；随后 ``setup``（:208-228）直接拿
``worktree_dir`` 当 cwd，只 catch ``TimeoutExpired`` → FileNotFoundError 逃逸成
引擎异常，keeper 重派一次命中一次。

本文件从编译产物 ``flows/issue-pipeline.flow.json`` 取节点（锁运行期真正执行的
定义），在真实 git 仓上重放「清树后重派」。修好后转绿（v0.3.1）：建树段换成
``wt_prep`` code 节点（复用 / prune / 复用残留分支重建 / 失败 → partial 回评），
setup 不向引擎抛未捕获异常。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

# plaita 已是本机可导入依赖（pyproject/editable），表达式求值用它跑生产同款语义。
from plaita.io import evaluate
from plaita.node.decide import Condition

FLOW_JSON = (pathlib.Path(__file__).resolve().parent.parent
             / "flows" / "issue-pipeline.flow.json")


def _nodes() -> dict:
    flow = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
    return {n["id"]: n for n in flow["nodes"] if n.get("id")}


def _ctx(main_clone, worktree_dir, setup_command="npm install"):
    return {
        "main_clone": str(main_clone),
        "worktree_dir": str(worktree_dir),
        "branch_name": "pipeline/issue-13",
        "base_branch": "main",
        "setup_command": setup_command,
        "setup_timeout_secs": 60,
    }


def _run_setup(ctx: dict, nodes: dict | None = None, prior: dict | None = None) -> dict:
    """按 setup 节点的声明跑它那段 code（生产节点在同款沙箱里执行同一段代码）。"""
    node = (nodes or _nodes())["setup"]
    scope: dict = {}
    exec(node["code"], scope)
    expr = {"$INPUT": ctx, "$NODE": prior or {}}
    inputs = {k: (evaluate(v, expr) if isinstance(v, str) else v)
              for k, v in (node.get("input") or {}).items()}
    return scope["run"](inputs)


def _git(*args, cwd=None, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True)
    if check:
        assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r


def _recycled_clone(tmp_path, number=13):
    """origin(裸) + clone，并留下残留分支 pipeline/issue-N：
    等价于「上一轮建过 worktree、跑完被清树」的现场（本次核查 agentproc 仓
    ``git branch`` 有 pipeline/issue-8|17，``.worktrees/`` 为空）。"""
    origin = tmp_path / "origin.git"
    main = tmp_path / "clone"
    _git("init", "-q", "--bare", str(origin))
    _git("clone", "-q", str(origin), str(main))
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "base.txt").write_text("base", encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "base", cwd=main)
    _git("push", "-q", "origin", "HEAD:main", cwd=main)
    _git("fetch", "-q", "origin", cwd=main)
    _git("branch", f"pipeline/issue-{number}", "origin/main", cwd=main)
    return main


def _is_worktree(path) -> bool:
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _run_prep_chain(ctx: dict, max_steps: int = 12) -> dict:
    """按编译产物顺序执行建树段（git_sync → wt_prep → setup）的 capture/code 节点。

    返回 ``{节点 id: 输出}``。走到失败回评的分支时置 ``steps["diverted"] = True``
    ——表示流程没把缺 cwd 的 setup 交给子进程，而是分叉去出害。
    """
    nodes = _nodes()
    steps: dict = {}
    node_id = "git_sync"
    for _ in range(max_steps):
        node = nodes.get(node_id)
        if node is None:
            break
        expr = {"$INPUT": ctx, "$NODE": dict(steps)}
        if node["type"] == "capture":
            cmd = [str(evaluate(el, expr)) if isinstance(el, str) else str(el)
                   for el in node["command"]]
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=node.get("timeout_secs", 120))
            steps[node_id] = {"ok": r.returncode == 0, "exit_code": r.returncode,
                              "stdout": r.stdout, "stderr": r.stderr}
        elif node["type"] == "code":
            scope: dict = {}
            exec(node["code"], scope)
            inputs = {k: (evaluate(v, expr) if isinstance(v, str) else v)
                      for k, v in (node.get("input") or {}).items()}
            steps[node_id] = scope["run"](inputs)
        elif node["type"] == "if":
            cond = node["condition"]
            hit = Condition(field=cond["field"], operator=cond["operator"],
                            value=cond["value"]).match(expr)
            if hit:
                steps["diverted"] = True
                break
            node_id = node.get("else_next")
            continue
        else:
            steps["diverted"] = True
            break
        if node_id == "setup":
            break
        node_id = node.get("next")
    return steps


# ── 复现 1：setup 的 cwd 已被回收 ─────────────────────────────────────

def test_wip_setup_missing_cwd_degrades_instead_of_raising(tmp_path):
    """worktree_dir 不存在时 setup 必须给出可读失败（ok=False），不得把
    FileNotFoundError 抛给引擎（现流程只 catch TimeoutExpired）。"""
    ctx = _ctx(tmp_path / "clone", tmp_path / "gone", setup_command="npm install")

    out = _run_setup(ctx)                      # 修好前：FileNotFoundError 逃逸

    assert out.get("ok") is False, "缺 cwd 时 setup 必须如实失败，不能静默跳过"
    assert out.get("note"), "失败原因要可读（回评模板取 note）"


# ── 复现 2：清树后重派（分支残留 + worktree 缺失）─────────────────────

def test_wip_redispatch_after_recycled_worktree_recovers(tmp_path):
    """重派时旧 wt_add 必然非 0（实测 255: 分支已存在），建树段必须自愈或如实降级。"""
    main = _recycled_clone(tmp_path)
    wt = main / ".worktrees" / "issue-13"
    ctx = _ctx(main, wt, setup_command="true")

    steps = _run_prep_chain(ctx)               # 修好前：setup 在此抛 FileNotFoundError

    assert (_is_worktree(wt) or steps.get("diverted")
            or (steps.get("setup") or {}).get("ok") is False), (
        "清树后重派：既没自愈建树，也没降级/分叉到回评路径——"
        f"wt_prep={steps.get('wt_prep')} setup={steps.get('setup')}")
    # 自愈到位 ⇒ 前置条件成立，setup 能在该 worktree 里跑通（= 能推进到 investigate）
    assert _is_worktree(wt), f"残留分支未复用重建：{steps.get('wt_prep')}"
    assert (steps.get("setup") or {}).get("ok") is True, (
        f"自愈后 setup 未跑通：{steps.get('setup')}")
    assert _run_setup(_ctx(main, wt, setup_command="true")).get("ok") is True


# ── 复现 3：worktree 仍注册在册，但目录被外部删除 ─────────────────────

def test_wip_stale_registration_missing_dir_is_pruned(tmp_path):
    """`git worktree list` 仍登记该路径、目录却已被删（残留注册）时必须先 prune
    再复用分支重建，不能因「already registered」直接降级成 partial。"""
    main = _recycled_clone(tmp_path)
    wt = main / ".worktrees" / "issue-13"
    _git("worktree", "add", str(wt), "pipeline/issue-13", cwd=main)
    shutil.rmtree(wt)                          # 目录被回收，注册项留在 .git/worktrees
    assert not wt.exists()
    assert "issue-13" in _git("worktree", "list", cwd=main).stdout

    steps = _run_prep_chain(_ctx(main, wt, setup_command="true"))

    assert _is_worktree(wt), f"残留注册未清理/未重建：{steps.get('wt_prep')}"
    body = json.dumps(steps.get("wt_prep"), ensure_ascii=False)
    assert "already registered" not in body, body


# ── 正向对照：干净现场（无残留分支/目录）建树段照旧可用 ────────────────

def test_wip_fresh_prep_chain_still_builds_worktree(tmp_path):
    """修自愈路径不得破坏首发：全新现场跑建树段必须得到可用 worktree。"""
    main = _recycled_clone(tmp_path)
    _git("branch", "-D", "pipeline/issue-13", cwd=main)   # 去掉残留分支 = 首次派发
    wt = main / ".worktrees" / "issue-13"
    ctx = _ctx(main, wt, setup_command="true")

    steps = _run_prep_chain(ctx)

    assert _is_worktree(wt), f"首次派发建树失败：{steps.get('wt_prep')}"
    assert (steps.get("setup") or {}).get("ok") is True


# ── 结构断言：编译产物里建树段已换成幂等的 wt_prep + 失败回评出口 ──────

def test_wip_compiled_artifact_has_idempotent_prep_and_failure_reply():
    """验收 ②：CAPTURE 的返回码不再被丢弃——wt_add capture 消失，代之以
    wt_prep code 节点；其 `ok == False` 分支必须走到 partial 终态回评。"""
    nodes = _nodes()

    assert nodes.get("wt_add") is None, "旧 wt_add capture 仍在（返回码无人消费）"
    prep = nodes.get("wt_prep")
    assert prep and prep["type"] == "code", "缺幂等建树节点 wt_prep"

    branch_if = [n for n in nodes.values()
                 if n.get("type") == "if"
                 and (n.get("condition") or {}).get("field") == "$NODE.wt_prep.ok"]
    assert branch_if, "wt_prep 的成功/失败判定没进流程"
    fail = branch_if[0]["next"]
    assert nodes[fail]["type"] == "agentrun", f"失败分支首跳应为回评 agentrun：{fail}"
    post = nodes[nodes[fail]["next"]]
    assert post["type"] == "github_comment"
    assert post["text"].startswith('$F.concat("<!-- issue-pipeline -->')
    end = nodes[post["next"]]
    assert end["type"] == "end" and end["output"]["status"] == "partial", end

    # 失败回评不得泄露本机路径（与既有 reply_setup_fail 同形态：只经消毒出害口）
    assert "不要出现任何本机路径" in nodes[fail]["prompt"]
