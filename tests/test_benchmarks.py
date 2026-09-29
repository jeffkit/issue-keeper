"""benchmarks（L4 数据集）测试：构建/auto-label/人工标注/评测打分（fake agent）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from issue_keeper import benchmarks as B
from issue_keeper import metrics as m


def _seed(tmp: Path, eid: str, repo: str, issue: int, status: str) -> None:
    month = time.strftime("%Y-%m")
    d = tmp / "metrics" / month
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{eid}.json").write_text(json.dumps({
        "schema": 1, "execution_id": eid, "repo": repo, "issue": issue,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "status": status,
        "ok": status == "done", "duration_secs": 100.0, "flow_version": "2.0.0",
        "nodes": [
            {"id": "deps", "type": "code", "status": "success", "duration_ms": 10,
             "output": {"deps_json": "[]"}},
            {"id": "parsed", "type": "parse_json", "status": "success",
             "duration_ms": 5, "verdict": "actionable" if status == "done" else "blocked"},
        ],
    }), encoding="utf-8")
    # 产物目录：00-issue.md + dispatch.json（构建器从这里取正文/标题）
    art = Path(f"~/.issue-keeper/pipeline/{repo.split('/')[-1]}-{issue}").expanduser()
    art.mkdir(parents=True, exist_ok=True)
    (art / "00-issue.md").write_text(
        f"issue 正文 /Users/kong/secret should be redacted #{issue}", encoding="utf-8")
    (art / "dispatch.json").write_text(json.dumps(
        {"title": f"标题 {issue}", "repo_full": repo, "issue_number": issue}),
        encoding="utf-8")


def test_build_triage_auto_labels_and_redacts(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "METRICS_DIR", tmp_path / "metrics")
    _seed(tmp_path, "r1", "jeffkit/a", 1, "done")
    _seed(tmp_path, "r2", "jeffkit/a", 2, "blocked")
    _seed(tmp_path, "r3", "jeffkit/a", 3, "partial")     # 无自动标签 → 人工队列

    out = B.build_triage(days=60, root=tmp_path / "benchmarks")
    assert out["count"] == 3
    cases = B.load_cases("triage", out["version"], tmp_path / "benchmarks")
    by_id = {c["id"]: c for c in cases}
    assert by_id["jeffkit-a-1"]["expected"] == "actionable"
    assert by_id["jeffkit-a-1"]["labeled_by"] == "auto"
    assert by_id["jeffkit-a-2"]["expected"] == "blocked"
    assert by_id["jeffkit-a-3"]["expected"] is None and by_id["jeffkit-a-3"]["labeled_by"] is None
    # 消毒：本机路径不进数据集
    assert "/Users/kong" not in by_id["jeffkit-a-1"]["body"]
    assert "[REDACTED-PATH]" in by_id["jeffkit-a-1"]["body"]
    # provenance
    assert by_id["jeffkit-a-1"]["provenance"]["flow_version"] == "2.0.0"
    # manifest
    manifest = json.loads(
        (tmp_path / "benchmarks" / "triage" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["versions"]["1"]["count"] == 3
    assert "eval_prompt" in manifest["versions"]["1"]


def test_second_build_creates_v2(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "METRICS_DIR", tmp_path / "metrics")
    _seed(tmp_path, "r1", "jeffkit/a", 1, "done")
    v1 = B.build_triage(days=60, root=tmp_path / "b")["version"]
    v2 = B.build_triage(days=60, root=tmp_path / "b")["version"]
    assert v1 == 1 and v2 == 2
    ds = B.list_datasets(tmp_path / "b")
    assert ds[0]["name"] == "triage" and ds[0]["latest_version"] == 2


def test_label_case_human_gold(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "METRICS_DIR", tmp_path / "metrics")
    _seed(tmp_path, "r1", "jeffkit/a", 1, "partial")   # 无自动标签
    out = B.build_triage(days=60, root=tmp_path / "b")
    res = B.label_case("triage", "jeffkit-a-1", "actionable", root=tmp_path / "b")
    assert res["labeled"] is True
    cases = B.load_cases("triage", out["version"], tmp_path / "b")
    assert cases[0]["expected"] == "actionable" and cases[0]["labeled_by"] == "human"


def test_eval_triage_scores_against_expected(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "METRICS_DIR", tmp_path / "metrics")
    _seed(tmp_path, "r1", "jeffkit/a", 1, "done")       # expected=actionable
    _seed(tmp_path, "r2", "jeffkit/a", 2, "blocked")    # expected=blocked
    B.build_triage(days=60, root=tmp_path / "b")

    class _FakeNode:
        def __init__(self, **kw):
            self.kw = kw
        def execute(self, exec_ctx):
            # 永远答 actionable：case1 应命中、case2 应 miss
            return {"text": '{"verdict": "actionable", "notes": "n"}'}

    import plaita_nodes.agent_run as ar
    monkeypatch.setattr(ar, "AgentRunNode", _FakeNode)
    score = B.eval_triage("triage", root=tmp_path / "b", limit=10)
    assert score["scored"] == 2
    assert score["accuracy"] == 0.5
    assert len(score["misses"]) == 1 and score["misses"][0]["expected"] == "blocked"
    # 得分进了 manifest
    manifest = json.loads((tmp_path / "b" / "triage" / "manifest.json").read_text("utf-8"))
    assert manifest["versions"]["1"]["scores"][-1]["accuracy"] == 0.5
