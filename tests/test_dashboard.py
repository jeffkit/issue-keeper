"""dashboard REST API 测试（FastAPI TestClient + 临时 db，不打网络）。"""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from issue_keeper.dashboard import create_app


def _client(tmp_path):
    app = create_app(str(tmp_path / "internal.db"), agent_label="dashboard-test")
    return TestClient(app)


class TestDashboardApi:
    def test_statuses_order(self, tmp_path):
        c = _client(tmp_path)
        r = c.get("/api/statuses")
        assert r.json() == ["inbox", "todo", "doing", "review", "done", "closed"]

    def test_create_list_move_comment_close(self, tmp_path):
        c = _client(tmp_path)

        # create
        r = c.post("/api/projects/p/issues", json={
            "title": "bug", "body": "x", "author": "alice", "actor_type": "human", "kind": "issue",
        })
        assert r.status_code == 200
        n = r.json()["number"]
        assert r.json()["status"] == "inbox"

        # list
        assert len(c.get("/api/projects/p/issues").json()) == 1

        # move
        r = c.post(f"/api/projects/p/issues/{n}/move", json={
            "to_status": "doing", "actor": "alice", "actor_type": "human", "comment": "开始",
        })
        assert r.json()["status"] == "doing"

        # comment
        r = c.post(f"/api/projects/p/issues/{n}/comments", json={
            "body": "我也遇到", "author": "bob", "actor_type": "human",
        })
        assert r.status_code == 200 and r.json()["author"] == "bob"

        # detail
        r = c.get(f"/api/projects/p/issues/{n}")
        d = r.json()
        assert len(d["comments"]) == 1
        assert len(d["history"]) == 2  # 初始 + move

        # close
        r = c.post(f"/api/projects/p/issues/{n}/close", json={"actor": "alice", "actor_type": "human"})
        assert r.json()["status"] == "closed"

    def test_projects_counts(self, tmp_path):
        c = _client(tmp_path)
        c.post("/api/projects/p/issues", json={"title": "a", "author": "x"})
        c.post("/api/projects/q/issues", json={"title": "b", "author": "x"})
        ps = {p["project"]: p for p in c.get("/api/projects").json()}
        assert ps["p"]["total"] == 1 and ps["q"]["total"] == 1

    def test_404_on_missing_issue(self, tmp_path):
        c = _client(tmp_path)
        assert c.get("/api/projects/p/issues/999").status_code == 404

    def test_invalid_move_status_400(self, tmp_path):
        c = _client(tmp_path)
        r = c.post("/api/projects/p/issues", json={"title": "a", "author": "x"})
        n = r.json()["number"]
        bad = c.post(f"/api/projects/p/issues/{n}/move", json={"to_status": "bogus"})
        assert bad.status_code == 400

    def test_agent_create_goes_to_todo(self, tmp_path):
        c = _client(tmp_path)
        r = c.post("/api/projects/p/issues", json={
            "title": "auto", "author": "alpha", "actor_type": "agent",
        })
        assert r.json()["status"] == "todo"

    def test_create_with_labels_echoed(self, tmp_path):
        c = _client(tmp_path)
        r = c.post("/api/projects/p/issues", json={
            "title": "a", "author": "x", "labels": ["bug", "ai"],
        })
        assert r.json()["labels"] == ["bug", "ai"]
        # 列表里也能拿到
        listed = c.get("/api/projects/p/issues").json()
        assert listed[0]["labels"] == ["bug", "ai"]

    def test_root_returns_index_or_hint(self, tmp_path):
        # 临时 db 路径下不会有 frontend/dist，但 dashboard 模块自带的 dist 检测
        # 是固定指向仓库 frontend/dist（构建后存在）。两种响应都接受。
        c = _client(tmp_path)
        r = c.get("/")
        assert r.status_code == 200
        # 构建过 dist → index.html（text/html）；否则 JSON 提示
        assert "html" in r.headers["content-type"] or r.headers["content-type"].startswith("application/json")

    def test_create_project_then_list_with_role(self, tmp_path):
        c = _client(tmp_path)
        r = c.post("/api/projects", json={
            "name": "proj-a", "agent_label": "a-agent", "cwd": "/x/a",
            "profile": "claude-code", "source": "internal", "role": "keeper",
        })
        assert r.status_code == 200
        assert r.json()["role"] == "keeper"
        # 出现在项目列表
        ps = {p["project"]: p for p in c.get("/api/projects").json()}
        assert ps["proj-a"]["agent_label"] == "a-agent"
        assert ps["proj-a"]["role"] == "keeper"
        # 出现在团队列表
        team = {m["agent_label"]: m for m in c.get("/api/team").json()}
        assert team["a-agent"]["role"] == "keeper"

    def test_create_project_rejects_empty_name(self, tmp_path):
        c = _client(tmp_path)
        r = c.post("/api/projects", json={"name": "  "})
        assert r.status_code == 400

    def test_patch_project_role_and_delete(self, tmp_path):
        c = _client(tmp_path)
        c.post("/api/projects", json={"name": "p", "agent_label": "a-agent", "cwd": "/x"})
        # 默认 agent
        assert {p["project"]: p for p in c.get("/api/projects").json()}["p"]["role"] == "agent"
        # PATCH 改成 keeper
        r = c.patch("/api/projects/p", json={"role": "keeper"})
        assert r.status_code == 200 and r.json()["role"] == "keeper"
        # PATCH 404
        assert c.patch("/api/projects/none", json={"role": "keeper"}).status_code == 404
        # DELETE
        assert c.delete("/api/projects/p").status_code == 200
        assert c.delete("/api/projects/p").status_code == 404
        assert all(p["project"] != "p" for p in c.get("/api/projects").json())

    def test_create_project_defaults_role_agent(self, tmp_path):
        c = _client(tmp_path)
        c.post("/api/projects", json={"name": "p", "agent_label": "a", "cwd": "/x"})
        ps = {p["project"]: p for p in c.get("/api/projects").json()}
        assert ps["p"]["role"] == "agent"


# ── Pipeline 观测面端点（L2）─────────────────────────────────────────

def _seed_metrics(tmp_path: Path) -> Path:
    """在临时 metrics 目录造两个 run：一成一败（含门失败）。"""
    import json
    root = tmp_path / "metrics"
    d = root / time.strftime("%Y-%m")
    d.mkdir(parents=True)
    (d / "run-a.json").write_text(json.dumps({
        "schema": 1, "execution_id": "run-a", "repo": "jeffkit/a", "issue": 1,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "status": "done", "ok": True,
        "duration_secs": 120.0, "flow_version": "2.0.0",
        "nodes": [
            {"id": "investigate", "type": "agentrun", "status": "success", "duration_ms": 5000,
             "model": "glm-52", "tokens": {"input": 100, "output": 30}},
            {"id": "gate", "type": "gate", "status": "success", "duration_ms": 800,
             "gate": "repo-tests", "passed": True, "exit_code": 0},
        ],
    }), encoding="utf-8")
    (d / "run-b.json").write_text(json.dumps({
        "schema": 1, "execution_id": "run-b", "repo": "jeffkit/a", "issue": 2,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "status": "partial", "ok": False,
        "duration_secs": 240.0, "flow_version": "2.0.0",
        "nodes": [
            {"id": "gate", "type": "gate", "status": "error", "duration_ms": 900,
             "gate": "test", "passed": False, "exit_code": 1, "fail_tail": "boom"},
        ],
    }), encoding="utf-8")
    return root


def test_pipeline_summary_endpoint(client, tmp_path, monkeypatch):
    from issue_keeper import metrics as m
    monkeypatch.setattr(m, "METRICS_DIR", _seed_metrics(tmp_path))
    r = client.get("/api/pipeline/summary?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["total_runs"] == 2
    assert body["success_rate"] == 0.5
    assert body["by_repo"]["jeffkit/a"]["gate_failures"] == {"test": 1}
    assert body["gate_failures"] == {"test": 1}


def test_pipeline_runs_and_detail(client, tmp_path, monkeypatch):
    from issue_keeper import metrics as m
    monkeypatch.setattr(m, "METRICS_DIR", _seed_metrics(tmp_path))
    r = client.get("/api/pipeline/runs?days=30&repo=jeffkit/a")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 2
    failed = next(x for x in runs if x["status"] == "partial")
    assert failed["gate_failed"] == "test"
    assert failed["segments"] == {"gate": 900}

    d = client.get("/api/pipeline/runs/run-b")
    assert d.status_code == 200
    assert d.json()["nodes"][0]["fail_tail"] == "boom"
    assert client.get("/api/pipeline/runs/missing").status_code == 404


from pytest import fixture  # noqa: E402


@fixture
def client(tmp_path):
    return _client(tmp_path)
