"""工作台（V1 派生视图）测试：阶段推导、缓存兜底、端点聚合。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from issue_keeper import workbench as wb
from issue_keeper.sources import Resource
from issue_keeper.state import save_state, State


@pytest.fixture(autouse=True)
def _clear_gh_cache():
    wb._GH_CACHE.clear()
    yield
    wb._GH_CACHE.clear()


def _res(repo: str, number: int, title: str = "t") -> Resource:
    return Resource(kind="issue", number=number, title=title, body="", state="open",
                    labels=[], author="alice", created_at="2026-09-30T10:00:00+0800",
                    updated_at="", status="inbox", actor_type="human")


class _FakeGH:
    def __init__(self, issues: list[Resource]):
        self.issues = issues
    def list_open(self, repo, kinds, labels=None):
        return self.issues


def _seed_metrics(tmp: Path, eid: str, repo: str, issue: int, status: str,
                  gate: str | None = None) -> None:
    d = tmp / time.strftime("%Y-%m")
    d.mkdir(parents=True, exist_ok=True)
    nodes = []
    if gate:
        nodes.append({"id": "gate", "type": "gate", "status": "error",
                      "duration_ms": 100, "gate": gate, "passed": False, "exit_code": 1})
    (d / f"{eid}.json").write_text(json.dumps({
        "schema": 1, "execution_id": eid, "repo": repo, "issue": issue,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "status": status,
        "ok": status == "done", "duration_secs": 60.0, "flow_version": "2.0.0",
        "nodes": nodes}), encoding="utf-8")


def _seed_lock(tmp: Path, repo: str, issue: int, alive_pid: int | None) -> None:
    slug = repo.split("/")[-1]
    lock = tmp / "pipeline" / f"{slug}-{issue}" / "run.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(alive_pid if alive_pid else 999999999), encoding="utf-8")


def _seed_state(tmp: Path, repo_slug: str, issue: int, *, blocked=False):
    st = State()
    it = st.repo(repo_slug).item(str(issue))
    it.blocked = blocked
    save_state(tmp / "state.json", st)


def test_stage_derivation_matrix(tmp_path):
    """doing > needs-human(六种终态) > blocked > settled > queued 的推导次序。"""
    md = tmp_path / "metrics"
    _seed_metrics(md, "r-done", "jeffkit/a", 1, "done")
    _seed_metrics(md, "r-partial", "jeffkit/a", 2, "partial", gate="test")
    _seed_metrics(md, "r-blocked", "jeffkit/a", 3, "blocked")
    _seed_metrics(md, "r-readonly", "jeffkit/a", 4, "readonly")
    _seed_metrics(md, "r-onhold", "jeffkit/a", 5, "onhold")
    _seed_metrics(md, "r-eng", "jeffkit/a", 6, "engine_error")

    issues = [_res("jeffkit/a", i) for i in range(1, 8)]
    wb_obj = wb.build_workbench(
        [{"name": "jeffkit/a", "source": "github_cli"}],
        metrics_dir=md, state_path=tmp_path / "absent.json",
        lock_root=tmp_path / "pipeline",
        gh_fetcher=lambda: _FakeGH(issues))

    g = wb_obj["groups"]
    assert [c["issue"] for c in g["needs-human"]] == [2, 5, 6]     # partial/onhold/engine_error
    assert [c["issue"] for c in g["blocked"]] == [3]
    assert sorted(c["issue"] for c in g["settled"]) == [1, 4]     # done/readonly
    assert [c["issue"] for c in g["queued"]] == [7]               # 无 run 记录
    partial = next(c for c in g["needs-human"] if c["issue"] == 2)
    assert partial["reason"] == "全量测试两轮未过"
    assert partial["last_run"]["gate_failed"] == "test"
    assert partial["url"].endswith("/issues/2")


def test_in_flight_beats_terminal_status(tmp_path):
    """run.lock 活着 → doing，即便上一轮终态是 partial。"""
    md = tmp_path / "metrics"
    _seed_metrics(md, "r1", "jeffkit/a", 9, "partial")
    _seed_lock(tmp_path, "jeffkit/a", 9, alive_pid=os.getpid())

    wb_obj = wb.build_workbench(
        [{"name": "jeffkit/a", "source": "github_cli"}],
        metrics_dir=md, state_path=tmp_path / "absent.json",
        lock_root=tmp_path / "pipeline",
        gh_fetcher=lambda: _FakeGH([_res("jeffkit/a", 9)]))
    doing = wb_obj["groups"]["doing"]
    assert len(doing) == 1 and doing[0]["in_flight"] is True
    assert doing[0]["running_since"] is not None


def test_screener_blocked_flag_to_needs_human(tmp_path):
    md = tmp_path / "metrics"
    _seed_state(tmp_path, "jeffkit-a", 5, blocked=True)
    wb_obj = wb.build_workbench(
        [{"name": "jeffkit/a", "source": "github_cli"}],
        metrics_dir=md, state_path=tmp_path / "state.json",
        lock_root=tmp_path / "pipeline",
        gh_fetcher=lambda: _FakeGH([_res("jeffkit/a", 5)]))
    nh = wb_obj["groups"]["needs-human"]
    assert len(nh) == 1 and "安全过滤" in nh[0]["reason"]


def test_gh_failure_uses_stale_cache(tmp_path):
    md = tmp_path / "metrics"
    ok = _FakeGH([_res("jeffkit/a", 1)])
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return ok
        raise RuntimeError("gh boom")

    t0 = time.time()
    wb.build_workbench([{"name": "jeffkit/a", "source": "github_cli"}],
                       metrics_dir=md, state_path=tmp_path / "s",
                       gh_fetcher=flaky, now=t0)
    # TTL（120s）过期后 fetcher 抛错 → stale 缓存兜底，不报错、仍有卡片
    wb2 = wb.build_workbench([{"name": "jeffkit/a", "source": "github_cli"}],
                             metrics_dir=md, state_path=tmp_path / "s",
                             gh_fetcher=flaky, now=t0 + wb.GH_CACHE_TTL + 1)
    assert len(wb2["groups"]["queued"]) == 1
    assert "stale" in wb2["errors"].get("jeffkit/a", "")


def test_internal_source_excluded(tmp_path):
    wb_obj = wb.build_workbench(
        [{"name": "ik-selftest", "source": "internal"},
         {"name": "jeffkit/a", "source": "github_cli"}],
        metrics_dir=tmp_path / "metrics", state_path=tmp_path / "s",
        gh_fetcher=lambda: _FakeGH([_res("jeffkit/a", 1)]))
    assert all(c["repo"] == "jeffkit/a"
               for cards in wb_obj["groups"].values() for c in cards)


from fastapi.testclient import TestClient  # noqa: E402
from issue_keeper.dashboard import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "internal.db"), agent_label="wb-test"))


def test_workbench_endpoint(client, tmp_path, monkeypatch):
    from issue_keeper import workbench as wb

    class _FakeSrc:
        def list_projects_meta(self):
            return [{"name": "jeffkit/a", "source": "github_cli", "cwd": "/x"}]
    monkeypatch.setattr("issue_keeper.dashboard.api._source",
                        lambda ctx: _FakeSrc())
    monkeypatch.setattr(wb, "build_workbench",
                        lambda bindings, **kw: {"groups": {"needs-human": [
                            {"repo": "jeffkit/a", "issue": 1, "title": "t",
                             "url": "u", "stage": "needs-human", "reason": "r"}]},
                            "counts": {"needs-human": 1}, "errors": {},
                            "generated_at": "now", "window_days": 45})
    r = client.get("/api/workbench?days=45")
    assert r.status_code == 200
    body = r.json()
    assert body["groups"]["needs-human"][0]["repo"] == "jeffkit/a"
