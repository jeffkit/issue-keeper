"""metrics 聚合器 + bridge MetricsRecorder 落盘的回归测试（L1 数据地基）。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# pipeline_bridge 模块级会 sys.path.insert plaita 根（其 tests/ 是常规包，会遮蔽
# 本仓 tests 命名空间，弄坏 test_reply 的 `from tests.test_config import`）——
# 导入完成后立即恢复 sys.path（已导入模块走 sys.modules 缓存，不受影响）。
_sys_path_before = set(sys.path)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "flows"))
import pipeline_bridge  # noqa: E402
sys.path[:] = [p for p in sys.path
               if p in _sys_path_before or p.endswith("/issue-keeper/flows")]

from pipeline_bridge import MetricsRecorder, _usage_tokens  # noqa: E402

from issue_keeper import metrics as m  # noqa: E402


class _FakeNode:
    def __init__(self, node_id: str, node_type: str):
        self.id = node_id
        self.node_type = node_type
        self.name = node_id


def _recorder(tmp_path: Path) -> MetricsRecorder:
    return MetricsRecorder(tmp_path / "metrics")


def test_recorder_persists_nodes_tokens_and_gates(tmp_path):
    rec = _recorder(tmp_path)
    rec.on_flow_start(flow=None)
    rec.on_node_start(flow=None, node=None)
    rec.on_node_end(flow=None, node=_FakeNode("investigate", "agentrun"),
                    result={"text": "DONE ok", "model": "glm-52", "cli": "claude-code",
                            "session_id": "s1",
                            "usage": {"input_tokens": 100, "output_tokens": 40}})
    rec.on_node_start(flow=None, node=None)
    rec.on_node_end(flow=None, node=_FakeNode("gate", "gate"),
                    result={"gate": "repo-tests", "passed": False, "exit_code": 1,
                            "stdout": "FAIL: cargo test boom"})
    rec.on_node_start(flow=None, node=None)
    rec.on_node_end(flow=None, node=_FakeNode("review", "agentrun"),
                    result={"text": "x" * 30, "usage": None},
                    error="Command timed out after 2700 seconds")

    out = rec.finalize({"execution_id": "exec-1", "repo": "jeffkit/recursive",
                        "issue": 7, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "status": "partial", "ok": False})

    # 落盘位置：<root>/<YYYY-MM>/<execution_id>.json
    month = time.strftime("%Y-%m")
    path = tmp_path / "metrics" / month / "exec-1.json"
    assert path.exists()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["repo"] == "jeffkit/recursive" and stored["issue"] == 7
    nodes = {n["id"]: n for n in stored["nodes"]}
    assert nodes["investigate"]["tokens"] == {"input": 100, "output": 40}
    assert nodes["investigate"]["model"] == "glm-52"
    assert nodes["gate"]["passed"] is False and nodes["gate"]["gate"] == "repo-tests"
    assert "cargo test boom" in nodes["gate"]["fail_tail"]
    assert nodes["review"]["timed_out"] is True and nodes["review"]["status"] == "error"
    assert out["nodes"] and out["duration_secs"] is not None


def test_recorder_fail_open_on_garbage(tmp_path):
    """观测永不阻塞主管线：垃圾输入只吞异常。"""
    rec = _recorder(tmp_path)
    rec.on_node_end(flow=None, node=None, result=None)  # 无 node_start 也不炸
    out = rec.finalize({})  # 无 execution_id → 不写文件但不抛
    assert isinstance(out, dict)


def test_usage_tokens_defensive():
    assert _usage_tokens({"input_tokens": 10, "output_tokens": 5}) == {"input": 10, "output": 5}
    assert _usage_tokens({"prompt_tokens": 7, "completion_tokens": 3}) == {"input": 7, "output": 3}
    assert _usage_tokens(None) is None
    assert _usage_tokens({"weird": "x"}) == {"input": 0, "output": 0}


def _seed(tmp: Path, execution_id: str, repo: str, status: str, dur: float,
          gates: list[tuple[str, bool]] | None = None, tokens: int = 0,
          started: str | None = None, version: str = "2.0.0") -> None:
    nodes = []
    for name, ok in (gates or []):
        nodes.append({"id": "gate", "type": "gate", "status": "success" if ok else "error",
                      "duration_ms": 100, "gate": name, "passed": ok, "exit_code": 0 if ok else 1})
    if tokens:
        nodes.append({"id": "investigate", "type": "agentrun", "status": "success",
                      "duration_ms": 500, "tokens": {"input": tokens, "output": 0}})
    month = (started or time.strftime("%Y-%m"))[:7]
    d = tmp / "metrics" / month
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{execution_id}.json").write_text(json.dumps({
        "schema": 1, "execution_id": execution_id, "repo": repo, "issue": 1,
        "started": started or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": status, "ok": status == "done", "duration_secs": dur,
        "flow_version": version, "nodes": nodes,
    }), encoding="utf-8")


def test_summarize_aggregates_by_repo(tmp_path):
    _seed(tmp_path, "r1", "jeffkit/a", "done", 100.0, gates=[("fmt", True)], tokens=50)
    _seed(tmp_path, "r2", "jeffkit/a", "partial", 300.0, gates=[("test", False)], tokens=70)
    _seed(tmp_path, "r3", "jeffkit/b", "done", 200.0)

    s = m.summarize(days=30, metrics_dir=tmp_path / "metrics")
    assert s["total_runs"] == 3
    assert s["success_rate"] == round(2 / 3, 3)
    a = s["by_repo"]["jeffkit/a"]
    assert a["runs"] == 2 and a["done"] == 1
    assert a["success_rate"] == 0.5
    assert a["duration_secs"]["p50"] == 200.0
    assert a["tokens"] == 120
    assert a["gate_failures"] == {"test": 1}
    assert s["gate_failures"] == {"test": 1}
    assert s["tokens_total"] == 120
    assert s["flow_versions"] == {"2.0.0": 3}
    # p90 全局
    assert s["duration_secs"]["p90"] == 300.0


def test_summarize_repo_filter_and_empty(tmp_path):
    _seed(tmp_path, "r1", "jeffkit/a", "done", 100.0)
    s = m.summarize(days=30, repo="jeffkit/b", metrics_dir=tmp_path / "metrics")
    assert s["total_runs"] == 0 and s["success_rate"] is None
    only_a = m.summarize(days=30, repo="jeffkit/a", metrics_dir=tmp_path / "metrics")
    assert only_a["total_runs"] == 1 and only_a["by_repo"]["jeffkit/a"]["done"] == 1


def test_segments_of_excludes_glue_nodes(tmp_path):
    _seed(tmp_path, "r1", "jeffkit/a", "done", 100.0, tokens=1)
    rec = m.run_detail("r1", metrics_dir=tmp_path / "metrics")
    rec["nodes"].append({"id": "_n1", "type": "if", "duration_ms": 5})
    segs = m.segments_of(rec)
    assert "investigate" in segs and "_n1" not in segs
