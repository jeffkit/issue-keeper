"""#17 回归测试——triage 结构化输出解析失败不得被当成「业务拒工」。

锚点：`flows/issue_pipeline_flow.py` 的 `parsed = PARSE_JSON(...)`（修复前
`default={"verdict": "blocked", ...}`，现为退出 choices 的 `"degraded"` +
`parse_ok != True` 降级分支）与 `reply_blocked` 出害口
（「为 GitHub issue 写简短中文评论（直接给正文）：暂不开工。」）。

病根：`parse_ok=False` 这条**基础设施失败**路径被 default 直接折叠成
`verdict="blocked"` → 落进业务出害口 → 发一条「暂不开工」评论并 `return
{"status": "blocked"}`。两处后果（issue #17 实证）：
  1. 台账 run status=blocked —— 与「依赖未就绪」同格，进 workbench「受阻」列、
     benchmarks auto-label 记成业务判定（benchmarks.py:54 done→actionable /
     blocked→blocked），即「把判不出当成判为否」（#5 同族病根）；
  2. 原始 triage 输出没有任何落盘，人只能「复核原始输出」而拿不到原文。

修复形状（本文件即验收，与 AGENTS.md「禁令」无冲突——不碰 screener、
不碰防循环 marker / 可见前缀）：
  1. 解析失败不再路由进 `reply_blocked`：`parsed.parse_ok != True` 时先容错
     重解析 / 重试一次，仍失败则带「triage 解析失败」注解继续（run status
     非 blocked），不得直接发业务「暂不开工」评论；
  2. 原始输出落盘 `<artifact_dir>/triage-raw.txt`，run 记录目录里查得到；
  3. 该 run 不得被搁置（既不继续管线、也不回队列的第三种状态=隐身搁置）。

跑法（与 `tests/test_issue15_gh_scan_timeout.py` 同形：驱动生产产物本身）：
本文件从 `flows/issue-pipeline.flow.json`（console/运行期真正执行的定义）取节点，
只把**有网络/副作用的节点**（agentrun / github_comment / capture / gate /
hitl / git_publish）换成假节点，code / parse_json 节点跑真的——所以
`triage-raw.txt` 的落盘与路由断言钉的都是生产节点，不是本文件的重实现。

修复后：本文件全绿（降级路径 status=readonly、triage-raw.txt 落盘、
回评明示「解析失败 / 基础设施故障」且不含「暂不开工」）。修复前全红
（status=blocked、无 triage-raw.txt、评论是「暂不开工」）。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any, ClassVar, Optional

import pytest

HERE = pathlib.Path(__file__).resolve().parent
FLOW_JSON = HERE.parent / "flows" / "issue-pipeline.flow.json"

sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita")
sys.path.insert(0, "/Users/kong/projects/infra4agent/plaita-nodes/src")

import plaita_nodes  # noqa: E402,F401
from plaita import Node  # noqa: E402
from plaita.core.callback import FlowCallback  # noqa: E402
from plaita.core.executor import FlowExecution  # noqa: E402
from plaita.core.flow import Flow  # noqa: E402
from plaita.node import (  # noqa: E402
    NodeRegistry,
    get_default_registry,
    register_code_node,
)

plaita_nodes.register_all()
register_code_node(default_backend="subprocess")


# ── 记录器 + 假节点（只有网络/副作用节点被替换）──────────────────────

class _Recorder:
    def __init__(self) -> None:
        self.agents: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.comments: list[str] = []
        self.outputs: dict[str, Any] = {}


class _NodeOutputs(FlowCallback):
    """节点终态输出采集——run 记录（metrics/台账 nodes）就是靠这个口径落盘的。"""

    def __init__(self, run: _Recorder) -> None:
        self._run = run

    def on_node_end(self, flow, node, result=None, error=None, exception=None, **kwargs) -> None:
        self._run.outputs[str(getattr(node, "id", "") or "node")] = result


RUN = _Recorder()
TRIAGE_TEXT = ""


class _Fake(Node):
    node_type: ClassVar[str] = ""


class FakeAgentRun(_Fake):
    """agentrun 假节点：只回放固定文本，但**忠实求值 prompt**（模板里的
    缺失键 / 脏表达式照旧在这里暴露，不掩盖提示词回归）。"""

    node_type: ClassVar[str] = "agentrun"

    agent: Optional[Any] = None
    repo: Optional[Any] = None
    timeout_secs: Optional[Any] = None
    prompt: Optional[Any] = None

    def execute(self, execution: Any) -> dict:
        prompt = execution.evaluate(self.prompt)
        RUN.agents.append(self.id)
        RUN.prompts.append((self.id, str(prompt)))
        if self.id == "triage":
            return {"text": TRIAGE_TEXT, "status": "success"}
        if self.id == "review":
            return {"text": '{"verdict":"approve","notes":"ok"}', "status": "success"}
        return {"text": "DONE 假节点", "status": "success"}


class FakeGithubComment(_Fake):
    node_type: ClassVar[str] = "github_comment"

    repo: Optional[Any] = None
    issue_number: Optional[Any] = None
    text: Optional[Any] = None
    artifact_dir: Optional[Any] = None
    dedup_marker: Optional[Any] = None
    footer: Optional[Any] = None

    def execute(self, execution: Any) -> dict:
        RUN.comments.append(str(execution.evaluate(self.text) or ""))
        return {"posted": True, "note": ""}


class FakeCapture(_Fake):
    node_type: ClassVar[str] = "capture"

    command: Optional[Any] = None
    timeout_secs: Optional[Any] = None

    def execute(self, execution: Any) -> dict:
        return {"stdout": "", "stderr": "", "returncode": 0}


class FakeGate(_Fake):
    node_type: ClassVar[str] = "gate"

    command: Optional[Any] = None
    gate_name: Optional[Any] = None
    cwd: Optional[Any] = None
    timeout_secs: Optional[Any] = None
    max_retries: Optional[Any] = None

    def execute(self, execution: Any) -> dict:
        return {"passed": True, "stdout": "", "exit_code": 0}


class FakeHitl(_Fake):
    node_type: ClassVar[str] = "hitl"

    message: Optional[Any] = None
    timeout_secs: Optional[Any] = None

    def execute(self, execution: Any) -> dict:
        return {"status": "replied"}


class FakeGitPublish(_Fake):
    node_type: ClassVar[str] = "git_publish"

    worktree_dir: Optional[Any] = None
    branch_name: Optional[Any] = None
    plan_file: Optional[Any] = None
    issue_number: Optional[Any] = None
    merge_mode: Optional[Any] = None
    main_clone: Optional[Any] = None
    base_branch: Optional[Any] = None

    def execute(self, execution: Any) -> dict:
        return {"pushed": True, "merged": True, "note": "假交付"}


def _registry() -> NodeRegistry:
    reg = NodeRegistry(parent=get_default_registry())
    for cls in (FakeAgentRun, FakeGithubComment, FakeCapture, FakeGate,
                FakeHitl, FakeGitPublish):
        reg.register(cls)
    return reg


def _git(*args: str, cwd: Optional[pathlib.Path] = None) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r


def _mini_clone(tmp_path: pathlib.Path) -> pathlib.Path:
    """origin(裸) + 单提交 clone——wt_prep 是真 code 节点（main 侧 #13 后置于
    investigate 之前），空目录会让它 fail → partial 早退，把降级路径整个短路。
    本地仓就是生产 main_clone 的最小等价物，不碰网络（git_sync 已是假节点）。"""
    origin = tmp_path / "origin.git"
    main = tmp_path / "main"
    _git("init", "-q", "--bare", str(origin))
    _git("clone", "-q", str(origin), str(main))
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "base.txt").write_text("base", encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "base", cwd=main)
    _git("push", "-q", "origin", "HEAD:main", cwd=main)
    _git("fetch", "-q", "origin", cwd=main)
    return main


# ── 驱动整条 flow（readonly=True：调查后早退，不碰 publish 副作用）──

def _run_flow(tmp_path: pathlib.Path, triage_text: str) -> tuple[dict, dict, _Recorder]:
    global TRIAGE_TEXT
    global RUN
    TRIAGE_TEXT = triage_text
    RUN = _Recorder()
    art = tmp_path / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    body = art / "00-issue.md"
    body.write_text("测试正文：本条不含 #N 依赖引用。\n", encoding="utf-8")
    main = _mini_clone(tmp_path)
    wt = tmp_path / "wt"

    ir = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
    flow = Flow.model_validate(ir, registry=_registry())
    execution = FlowExecution(callback_handlers=[_NodeOutputs(RUN)])
    result = execution.run_compatible(
        flow, False,
        repo_full="jeffkit/issue-keeper",
        issue_number=17,
        title="triage 解析失败时不应按业务拒工",
        author="okguitar",
        body_file=str(body),
        screener_verdict="safe",
        main_clone=str(main),
        worktree_dir=str(wt),
        branch_name="pipeline/issue-17",
        artifact_dir=str(art),
        base_branch="main",
        setup_command="",
        setup_timeout_secs=60,
        test_command="true",
        gate_timeout_secs=60,
        readonly=True,
        review_mode="auto",
        push_mode="none",
        triage_notes="",
        review_notes="",
        doc_notes="",
        investigate_timeout=60,
        plan_timeout=60,
        implement_timeout=60,
        review_timeout=60,
        fix_review_timeout=60,
        fix_test_timeout=60,
        document_timeout=60,
    )
    return result, {"artifact_dir": art}, RUN


# 三种「不可解析」形态（issue #17 原文：截断 / 非 JSON / 正文含花括号噪声）
BAD_TRIAGE = [
    pytest.param(
        '{"kind":"bug","verdict":"actionable","blockers":"","risk":"low",'
        '"acceptance":["a","b"',
        id="truncated-json",
    ),
    pytest.param(
        "无法给出结构化结论：这次分诊只查到了历史评论，需要人工再看一遍。",
        id="non-json-prose",
    ),
    pytest.param(
        "旧格式串仍在：preset resolves to: type={}, model={}；后面没有可解析的 JSON 对象。",
        id="braces-noise",
    ),
]


def test_parse_ok_control_group_still_flows_through(tmp_path):
    """控制组：可解析的 triage 输出照常进管线（上面三个用例并非恒红）。"""
    good = ('{"kind":"bug","verdict":"actionable","blockers":"","risk":"low",'
            '"acceptance":["a"],"commit_message":"fix: x (#17)","notes":"ok"}')
    result, _ctx, run = _run_flow(tmp_path, good)

    assert "reply_blocked" not in run.agents
    assert "investigate" in run.agents
    assert result.get("status") == "readonly"


@pytest.mark.parametrize("triage_text", BAD_TRIAGE)
def test_unparseable_triage_is_not_business_refusal(tmp_path, triage_text):
    """解析失败 → 不得落进「暂不开工」出害口，status 不得是业务拒工值 blocked。"""
    result, _ctx, run = _run_flow(tmp_path, triage_text)

    assert "reply_blocked" not in run.agents, \
        f"解析失败仍路由进业务拒工出害口；prompts={run.prompts}"
    assert result.get("status") != "blocked", \
        f"解析失败被记成业务拒工（status=blocked）：{result}"
    assert not any("暂不开工" in c for c in run.comments), \
        f"解析失败发出了业务「暂不开工」评论：{run.comments}"
    assert any("<!-- issue-pipeline -->" in c and "解析失败" in c and "暂不开工" not in c
               for c in run.comments), \
        f"解析失败路径没有明示基础设施故障的评论：{run.comments}"


@pytest.mark.parametrize("triage_text", BAD_TRIAGE)
def test_unparseable_triage_dumps_raw_output(tmp_path, triage_text):
    """原始 triage 输出必须落盘到 run 记录目录（可复现解析路径）。"""
    _result, ctx, _run = _run_flow(tmp_path, triage_text)

    raw = ctx["artifact_dir"] / "triage-raw.txt"
    assert raw.exists(), f"解析失败未落盘原始输出（{raw} 不存在）"
    assert triage_text in raw.read_text(encoding="utf-8")


@pytest.mark.parametrize("triage_text", BAD_TRIAGE)
def test_unparseable_triage_annotated_and_not_stranded(tmp_path, triage_text):
    """注解可观察，且该 run 不被搁置（继续管线 或 带注解回队列）。

    「继续管线」= investigate 段被走到；「回队列」= status=retry-later
    （keeper 侧不消费、自动重派，见 keeper.py 收尾分流）。两者都不满足即
    本次 run 既没结论也没回到队列——正是 #17 要消灭的搁置。
    """
    result, ctx, run = _run_flow(tmp_path, triage_text)

    raw = ctx["artifact_dir"] / "triage-raw.txt"
    evidence = json.dumps(
        {"comments": run.comments, "prompts": run.prompts, "nodes": run.outputs,
         "triage_raw": raw.read_text(encoding="utf-8") if raw.exists() else ""},
        ensure_ascii=False, default=str,
    )
    assert "解析失败" in evidence, "解析失败路径没有任何可观察的『triage 解析失败』注解"

    continued = "investigate" in run.agents
    requeued = result.get("status") == "retry-later"
    assert continued or requeued, \
        f"解析失败后既没继续管线也没回队列（status={result.get('status')!r}）——单被搁置"
