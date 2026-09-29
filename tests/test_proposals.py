"""proposals（L3 经验闭环）测试：规则生成去重 + 数值补丁应用与回滚。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from issue_keeper import metrics as m
from issue_keeper import proposals as P


def _seed_run(tmp: Path, eid: str, repo: str, nodes: list[dict],
              started: str | None = None) -> None:
    month = (started or time.strftime("%Y-%m"))[:7]
    d = tmp / month
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{eid}.json").write_text(json.dumps({
        "schema": 1, "execution_id": eid, "repo": repo, "issue": 1,
        "started": started or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "partial", "ok": False, "duration_secs": 100.0,
        "flow_version": "2.0.0", "nodes": nodes,
    }), encoding="utf-8")


def test_generate_gate_timeout_and_dedup(tmp_path):
    md = tmp_path / "metrics"
    _seed_run(md, "r1", "jeffkit/a", nodes=[
        {"id": "gate", "type": "gate", "status": "error", "duration_ms": 100,
         "gate": "test", "passed": False, "exit_code": 124},
    ])
    _seed_run(md, "r2", "jeffkit/a", nodes=[
        {"id": "gate", "type": "gate", "status": "error", "duration_ms": 100,
         "gate": "test", "passed": False, "exit_code": 124},
    ])
    import issue_keeper.metrics as _m
    _runs = _m.iter_runs(days=7, metrics_dir=md)
    print("DBG runs:", [(r.get("execution_id"), r.get("nodes")) for r in _runs])
    print("DBG files:", list(md.glob("*/*.json")))
    created = P.generate(days=7, metrics_dir=md, dir_path=tmp_path / "proposals")
    assert len(created) == 1
    p = created[0]
    assert p["repo"] == "jeffkit/a" and p["kind"] == "raise_gate_timeout" and p["target"] == "test"
    # 重复 generate 不再产生同类 pending 提案
    again = P.generate(days=7, metrics_dir=md, dir_path=tmp_path / "proposals")
    assert again == []


def test_generate_segment_timeout_requires_two_hits(tmp_path):
    md = tmp_path / "metrics"
    nodes_timeout = [{"id": "implement", "type": "agentrun", "status": "error",
                      "duration_ms": 4200000, "timed_out": True}]
    _seed_run(md, "r1", "jeffkit/a", nodes=nodes_timeout)
    assert P.generate(days=7, metrics_dir=md, dir_path=tmp_path / "p1") == []
    _seed_run(md, "r2", "jeffkit/a", nodes=nodes_timeout)
    created = P.generate(days=7, metrics_dir=md, dir_path=tmp_path / "p1")
    assert [p["kind"] for p in created] == ["raise_segment_timeout"]
    assert created[0]["target"] == "implement"


def test_generate_gate_never_passes_is_manual(tmp_path):
    md = tmp_path / "metrics"
    for i in range(3):
        _seed_run(md, f"r{i}", "jeffkit/a", nodes=[
            {"id": "gate", "type": "gate", "status": "error", "duration_ms": 100,
             "gate": "smoke", "passed": False, "exit_code": 1},
        ])
    created = P.generate(days=7, metrics_dir=md, dir_path=tmp_path / "p")
    assert [p["kind"] for p in created] == ["gate_never_passes"]
    assert created[0]["status"] == "manual"


CONFIG_TEMPLATE = '''internal_db: {tmp}/internal.db
screener:
  enabled: false
pipeline_repos:
  "jeffkit/a":
    base_branch: main
    timeout_overrides:
      implement: 1800
    gates:
      - name: test
        command: pytest -q
        timeout_secs: 600
'''


def _write_config(tmp: Path) -> Path:
    cfg = tmp / "config.yaml"
    cfg.write_text(CONFIG_TEMPLATE.format(tmp=tmp), encoding="utf-8")
    return cfg


def test_apply_gate_timeout_patch_and_validate(tmp_path):
    md = tmp_path / "metrics"
    _seed_run(md, "r1", "jeffkit/a", nodes=[
        {"id": "gate", "type": "gate", "status": "error", "duration_ms": 100,
         "gate": "test", "passed": False, "exit_code": 124},
    ])
    P.generate(days=7, metrics_dir=md, dir_path=tmp_path / "proposals")
    pid = P.list_proposals(dir_path=tmp_path / "proposals")[0]["id"]
    cfg = _write_config(tmp_path)

    out = P.apply_proposal(pid, str(cfg), factor=1.5, dir_path=tmp_path / "proposals")
    assert out["applied"] is True
    text = cfg.read_text(encoding="utf-8")
    assert "timeout_secs: 900" in text  # 600 × 1.5
    # 应用后配置仍能通过 load_config（脚手架：screener.enabled 显式关闭）
    from issue_keeper.config import load_config
    load_config(str(cfg))
    prop = json.loads((tmp_path / "proposals" / f"{pid}.json").read_text(encoding="utf-8"))
    assert prop["status"] == "applied" and "bak-proposal" in prop["backup"]


def test_apply_segment_timeout_inserts_override(tmp_path):
    md = tmp_path / "metrics"
    for i in range(2):
        _seed_run(md, f"r{i}", "jeffkit/a", nodes=[
            {"id": "review", "type": "agentrun", "status": "error",
             "duration_ms": 2700000, "timed_out": True},
        ])
    P.generate(days=7, metrics_dir=md, dir_path=tmp_path / "proposals")
    pid = P.list_proposals(dir_path=tmp_path / "proposals")[0]["id"]
    cfg = _write_config(tmp_path)

    out = P.apply_proposal(pid, str(cfg), dir_path=tmp_path / "proposals")
    assert out["applied"] is True
    text = cfg.read_text(encoding="utf-8")
    assert "review: 900" in text  # 无显式值 → 基线 900 占位
    from issue_keeper.config import load_config
    load_config(str(cfg))


def test_reject_and_manual_not_applicable(tmp_path):
    md = tmp_path / "metrics"
    for i in range(3):
        _seed_run(md, f"r{i}", "jeffkit/a", nodes=[
            {"id": "gate", "type": "gate", "status": "error", "duration_ms": 100,
             "gate": "smoke", "passed": False, "exit_code": 1},
        ])
    P.generate(days=7, metrics_dir=md, dir_path=tmp_path / "proposals")
    pid = P.list_proposals(dir_path=tmp_path / "proposals")[0]["id"]
    cfg = _write_config(tmp_path)
    out = P.apply_proposal(pid, str(cfg), dir_path=tmp_path / "proposals")
    assert out["applied"] is False and "manual" in out["reason"]
    P.reject_proposal(pid, dir_path=tmp_path / "proposals")
    assert P.list_proposals(status="rejected", dir_path=tmp_path / "proposals")
