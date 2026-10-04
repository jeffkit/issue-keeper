"""#11 回归：CI 修复通道——guard 的 `.github/` 禁改名单与合法 CI 修复单互斥。

选定通道 = issue label `ci-fix` → keeper payload `ci_fix` bool：guard 对 `ci_fix=true`
放行 `.github/workflows/**`（其余 `.github/**` 与仓外路径仍拦），plan / implement / review
三段提示词共享 `ci_note` 赋值节点的唯一措辞。

1. `ci_fix` 为真 → guard 放行 `.github/workflows/**`；
2. 未声明 → 依旧拦（负向），含未跟踪的新增文件（旧实现的绕过口）；
3. 已声明但改 `.github/` 下非 workflows 文件 → 依旧拦（放行边界只到 workflows）；
4. plan / implement / review 三段提示词都写明 `ci-fix` 通道（直接写或引用共享节点）；
5. keeper 把 label `ci-fix` 翻译成 payload `ci_fix=true`。

测试直接从编译产物 `flows/issue-pipeline.flow.json` 取 guard 的 code 与三段提示词——
锁的是 console/bridge 运行期真正执行的定义（同 test_flow_verdict_parser.py 的做法）。
"""
import json
import pathlib
import subprocess
import time

FLOW_JSON = (pathlib.Path(__file__).resolve().parent.parent
             / "flows" / "issue-pipeline.flow.json")


def _node(node_id: str) -> dict:
    flow = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
    return next(n for n in flow["nodes"] if n.get("id") == node_id)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _repo_with_edits(tmp_path, *paths: str) -> pathlib.Path:
    """建一个 git 仓并提交基线，然后按 paths 追加改动（相对 HEAD 的未提交 diff）。"""
    wt = tmp_path / "wt"
    (wt / ".github" / "workflows").mkdir(parents=True)
    (wt / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (wt / ".github" / "dependabot.yml").write_text("version: 2\n", encoding="utf-8")
    (wt / "README.md").write_text("base\n", encoding="utf-8")
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.com")
    _git(wt, "config", "user.name", "t")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    for p in paths:
        f = wt / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("changed\n", encoding="utf-8")
    return wt


def _guard(worktree, **inputs) -> dict:
    node = _node("guard")
    ns: dict = {}
    exec(node["code"], ns)  # noqa: S102 —— 锁运行期定义，源出本仓
    payload = {"worktree_dir": str(worktree)}
    payload.update(inputs)
    return ns["run"](payload)


# ── 1. 放行路径：声明 ci-fix 后 .github/workflows/** 不再是越界 ────────

def test_guard_allows_workflow_edit_when_ci_fix_declared(tmp_path):
    wt = _repo_with_edits(tmp_path, ".github/workflows/ci.yml")
    out = _guard(wt, ci_fix=True)
    assert out["violations"] == [], f"ci-fix 声明后 workflows 仍被判越界: {out}"
    assert out["ok"] is True


def test_guard_node_declares_ci_fix_input():
    """guard 必须接收 ci_fix（否则 INPUT 传不进来，放行永不生效）。"""
    node = _node("guard")
    assert node["input"].get("ci_fix") == "$INPUT.ci_fix"
    assert "ci_fix" in node["code"]


# ── 2. 负向：未声明照样拦 ──────────────────────────────────────────

def test_guard_blocks_workflow_edit_without_declaration(tmp_path):
    wt = _repo_with_edits(tmp_path, ".github/workflows/ci.yml")
    out = _guard(wt)
    assert out["ok"] is False and out["violations"] == [".github/workflows/ci.yml"]


def test_guard_sees_untracked_workflow_without_declaration(tmp_path):
    """新增（未 `git add`）的 workflow 也必须进名单——`git diff HEAD` 看不见 untracked，
    旧实现整段绕过 guard（`ok=True`）。"""
    wt = _repo_with_edits(tmp_path, ".github/workflows/evil.yml")
    out = _guard(wt)
    assert ".github/workflows/evil.yml" in out["violations"], out
    assert out["ok"] is False


def test_guard_allows_untracked_workflow_when_declared(tmp_path):
    wt = _repo_with_edits(tmp_path, ".github/workflows/new-ci.yml")
    out = _guard(wt, ci_fix=True)
    assert out["violations"] == [] and out["ok"] is True


# ── 3. 负向边界：只放行 workflows，不放行整套 .github ────────────────

def test_guard_still_blocks_other_github_paths_even_when_declared(tmp_path):
    wt = _repo_with_edits(tmp_path, ".github/dependabot.yml")
    out = _guard(wt, ci_fix=True)
    assert out["ok"] is False and out["violations"] == [".github/dependabot.yml"]


def test_guard_declared_mixed_edits_flagged_only_for_non_workflow(tmp_path):
    wt = _repo_with_edits(tmp_path, ".github/workflows/ci.yml", "README.md",
                          ".github/dependabot.yml")
    out = _guard(wt, ci_fix=True)
    assert out["violations"] == [".github/dependabot.yml"]
    assert out["ok"] is False


# ── 4. 三处口径一致：plan / implement / review 都能看到 ci-fix 通道 ──

def _with_referenced_nodes(prompt: str) -> str:
    """prompt + 它引用的 $NODE.<id> 节点全文。

    三段提示词要么自己写明 ci-fix，要么引用同一个 `$NODE.*` 事实源（放行措辞只
    维护一处）——两种写法都算口径一致，这里只要求事实可达。"""
    import re
    flow = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
    out = prompt
    for nid in set(re.findall(r"\$NODE\.([A-Za-z0-9_]+)", prompt)):
        node = next((n for n in flow["nodes"] if n.get("id") == nid), None)
        if node is not None:
            out += " " + json.dumps(node, ensure_ascii=False)
    return out


def test_prompts_document_ci_fix_channel():
    for node_id in ("plan", "implement", "review"):
        text = _with_referenced_nodes(_node(node_id)["prompt"])
        assert "ci-fix" in text, f"{node_id} 段看不到 ci-fix 通道（与放行通道互斥）"


# ── 5. keeper 侧：label `ci-fix` → payload ci_fix=true ──────────────

def _res(number=7, labels=()):
    from issue_keeper.sources import Resource
    return Resource(
        kind="issue", number=number, title="t", body="正文", state="open",
        labels=list(labels), author="bob", created_at="", updated_at="",
        status="inbox", actor_type="human",
    )


def _await_seen(art: pathlib.Path, number: int) -> dict:
    seen_path = art / "dispatch.json.seen.json"
    deadline = time.time() + 5
    while not seen_path.exists() and time.time() < deadline:
        time.sleep(0.05)
    return json.loads(seen_path.read_text(encoding="utf-8"))


def _dispatch(tmp_path, monkeypatch, labels):
    from issue_keeper.config import Config, PipelineRepoConfig, RepoBinding
    from issue_keeper.keeper import _dispatch_pipeline
    from issue_keeper.state import ItemState

    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = tmp_path / "bridge.py"
    bridge.write_text(
        "import shutil, sys\nshutil.copy(sys.argv[1], sys.argv[1] + '.seen.json')\n",
        encoding="utf-8",
    )
    cfg = Config(pipeline_bridge=str(bridge), pipeline_timeout_secs=30,
                 pipeline_push_mode="branch", pipeline_review_mode="auto",
                 pipeline_test_commands={}, pipeline_claim_comment=False)
    _dispatch_pipeline(
        cfg, RepoBinding(repo="a/b", profile="p"), _res(7, labels), ItemState(),
        "a/b issue#7", pc=PipelineRepoConfig(test_command="pytest"),
    )
    return _await_seen(tmp_path / ".issue-keeper" / "pipeline" / "b-7", 7)


def test_dispatch_payload_marks_ci_fix_for_labeled_issue(tmp_path, monkeypatch):
    seen = _dispatch(tmp_path, monkeypatch, labels=["ci-fix"])
    assert seen["ci_fix"] is True


def test_dispatch_payload_ci_fix_false_without_label(tmp_path, monkeypatch):
    seen = _dispatch(tmp_path, monkeypatch, labels=[])
    assert seen["ci_fix"] is False


def test_declares_ci_fix_label_case_and_space_insensitive():
    from issue_keeper.keeper import _declares_ci_fix
    assert _declares_ci_fix([" CI-Fix "]) is True


def test_declares_ci_fix_false_for_other_labels():
    from issue_keeper.keeper import _declares_ci_fix
    assert _declares_ci_fix(None) is False
    assert _declares_ci_fix(["bug"]) is False
