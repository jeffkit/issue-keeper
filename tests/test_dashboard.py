"""dashboard REST API 测试（FastAPI TestClient + 临时 db，不打网络）。"""

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
