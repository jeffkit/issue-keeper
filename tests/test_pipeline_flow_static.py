"""issue #1 — pipeline flow 静态断言（不跑 plaita 运行时）。

覆盖：worktree origin 基线（basemismatch）、reply gitfacts 事实注入、
triage 依赖落地门、编译产物与源码一致。
"""

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FLOW_SRC = (ROOT / "flows" / "issue_pipeline_flow.py").read_text(encoding="utf-8")
FLOW_JSON = ROOT / "flows" / "issue-pipeline.flow.json"


class TestOriginBaseline:
    def test_worktree_add_uses_origin_start_point(self):
        assert "'worktree', 'add', wd, '-b', br, 'origin/' + default" in FLOW_SRC

    def test_fetch_and_revparse_compare(self):
        assert "['git', 'fetch', 'origin']" in FLOW_SRC
        assert "['git', 'rev-parse', default]" in FLOW_SRC
        assert "['git', 'rev-parse', 'origin/' + default]" in FLOW_SRC
        assert "symbolic-ref" in FLOW_SRC

    def test_basemismatch_early_exit(self):
        assert '"status": "basemismatch"' in FLOW_SRC

    def test_keeper_routes_basemismatch_to_todo(self):
        src = (ROOT / "issue_keeper" / "keeper.py").read_text(encoding="utf-8")
        assert "basemismatch" in src


class TestGitfactsInjection:
    def test_gitfacts_node_exists(self):
        assert "ls-remote" in FLOW_SRC
        assert "branch', '-r', '--contains'" in FLOW_SRC
        assert "in_origin_main" in FLOW_SRC

    def test_reply_prompt_injects_gitfacts(self):
        assert "$NODE.gitfacts.branch_on_origin" in FLOW_SRC
        assert "$NODE.gitfacts.in_origin_main" in FLOW_SRC

    def test_reply_prompt_forbids_inference(self):
        assert "不得根据任何自述或过程记录推断落地状态" in FLOW_SRC


class TestDependencyGate:
    def test_triage_prompt_has_hard_rule(self):
        assert "依赖落地门" in FLOW_SRC
        assert "禁止就地实现依赖" in FLOW_SRC

    def test_triage_inputs_declared(self):
        assert "$INPUT.dependency_issues" in FLOW_SRC
        assert "$INPUT.umbrella_body_file" in FLOW_SRC

    def test_keeper_parses_dependency_line(self):
        src = (ROOT / "issue_keeper" / "keeper.py").read_text(encoding="utf-8")
        assert "依赖" in src and "dependency_issues" in src
        assert "umbrella_body_file" in src


class TestCompiledJson:
    def test_json_matches_source(self):
        before = FLOW_JSON.read_bytes()
        subprocess.run(
            [sys.executable, str(ROOT / "flows" / "build_issue_pipeline.py")],
            check=True, capture_output=True,
        )
        assert FLOW_JSON.read_bytes() == before, "编译产物与源码不一致（需重跑 build）"

    def test_json_has_normalized_keys(self):
        ir = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
        ends = [n for n in ir.get("nodes", []) if n.get("type") == "end"]
        assert ends
        for n in ends:
            s = json.dumps(n, ensure_ascii=False)
            assert '"posted"' not in s, f"end 节点仍有 posted 键: {n.get('id')}"
