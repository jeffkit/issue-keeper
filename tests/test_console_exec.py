"""engine=v2-console（G5/G6）回归：console 客户端 / verdict 映射 / 派发与收尾接线。

契约（issue-keeper docs/DESIGN-console-execution.md §G5/G6 + §5.6）：
- 派发 = POST /api/executions，在途锚 = console-exec.json（daemon 重启不丢）；
- 台账写入迁移（D5）= reaper 从 execution 终态映射落账，bridge 不再是唯一写方；
- 决策表：error → resume-retry ×1（G1 断点步进）；running + 心跳超阈 → zombie
  cancel + engine_error 行；retry 后仍 error → 落 engine_error 行走既有升级；
- 非 engine_error 的收尾回评由 keeper 出（console flow 无回评节点契约）。
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_keeper import console_exec as ce
from issue_keeper import keeper as K
from issue_keeper.config import (Config, GateSpec, PipelineConsoleConfig,
                                 PipelineConfig, RepoBinding)
from issue_keeper.sources import Resource
from issue_keeper.state import ItemState, State


# ---------- console_exec 纯函数 ----------


class TestVerdictFromExecution:
    def test_completed_with_verdict_node(self):
        detail = {"status": "completed",
                  "context": {"$NODE": {"verdict": {"verdict": "committed", "via": "git-publish"}}}}
        assert ce.verdict_from_execution(detail)["verdict"] == "committed"

    def test_completed_without_verdict_is_engine_error(self):
        detail = {"status": "completed", "context": {"$NODE": {"a": {"ok": True}}}}
        out = ce.verdict_from_execution(detail)
        assert out["verdict"] == "engine_error"
        assert "verdict" in out["why"]

    def test_error_carries_message(self):
        out = ce.verdict_from_execution({"status": "error",
                                         "error": {"message": "AgentRunError: boom"}})
        assert out == {"verdict": "engine_error", "why": "AgentRunError: boom"}

    def test_cancelled_and_suspended(self):
        assert ce.verdict_from_execution({"status": "cancelled"})["verdict"] == "engine_error"
        out = ce.verdict_from_execution({"status": "suspended"})
        assert "suspended" in out["why"]


class TestMapVerdict:
    def test_committed(self):
        row = ce.map_verdict({"verdict": "committed", "via": "git-publish"})
        assert row["status"] == "done" and row["pushed"] and row["merged"]

    def test_skip_commit(self):
        assert ce.map_verdict({"verdict": "skip-commit"})["status"] == "done"

    def test_retry_later_and_failed(self):
        assert ce.map_verdict({"verdict": "retry-later", "stage": "preflight"})["status"] == "retry-later"
        row = ce.map_verdict({"verdict": "failed-preserved", "stage": "gates", "why": "x"})
        assert row["status"] == "failed" and row["stage"] == "gates"

    def test_unknown_is_engine_error(self):
        assert ce.map_verdict({"verdict": "whatever"})["status"] == "engine_error"


class TestZombie:
    def _detail(self, age_secs, status="running"):
        ts = (datetime.now() - timedelta(seconds=age_secs)).isoformat()
        return {"status": status, "last_update_time": ts}

    def test_old_running_is_zombie(self):
        assert ce.zombie(self._detail(7201), 7200) is True

    def test_fresh_running_is_not(self):
        assert ce.zombie(self._detail(60), 7200) is False

    def test_terminal_never_zombie(self):
        assert ce.zombie(self._detail(99999, status="completed"), 7200) is False


class TestClientErrors:
    def test_404_maps_to_not_found(self, monkeypatch):
        import urllib.error

        def _raise(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

        monkeypatch.setattr(ce.urllib.request, "urlopen", _raise)
        c = ce.ConsoleExecClient("http://x", "k")
        with pytest.raises(ce.ConsoleExecNotFound):
            c.get_execution("e1")

    def test_unreachable_maps_to_unavailable(self, monkeypatch):
        import urllib.error

        monkeypatch.setattr(ce.urllib.request, "urlopen",
                            lambda req, timeout: (_ for _ in ()).throw(
                                urllib.error.URLError("conn refused")))
        c = ce.ConsoleExecClient("http://x", "k")
        with pytest.raises(ce.ConsoleExecUnavailable):
            c.get_execution("e1")

    def test_start_returns_execution_id(self, monkeypatch):
        monkeypatch.setattr(ce.ConsoleExecClient, "_request",
                            lambda self, m, p, b=None: {"execution_id": "abc"})
        assert ce.ConsoleExecClient("http://x", "k").start_execution("f", {}) == "abc"


# ---------- keeper 接线 ----------


class FakeClient:
    def __init__(self, detail=None, fail_start=None):
        self.detail = detail or {}
        self.fail_start = fail_start
        self.started = []
        self.resumed = []
        self.cancelled = []

    def start_execution(self, flow_id, params):
        if self.fail_start:
            raise ce.ConsoleExecError(self.fail_start)
        self.started.append((flow_id, params))
        return "exec-123"

    def get_execution(self, eid):
        return self.detail

    def resume(self, eid, resume_type, data=None):
        self.resumed.append((eid, resume_type))
        return {}

    def cancel(self, eid):
        self.cancelled.append(eid)
        return {}


def _cfg(console=True, **over) -> Config:
    base = dict(
        pipeline=PipelineConfig(console=PipelineConsoleConfig(
            url="http://127.0.0.1:8123", api_key="k", flow_id="issue-pipeline")),
        console_zombie_secs=7200,
        console_retry_max=1,
        pipeline_timeout_secs=30,
    )
    base.update(over)
    return Config(**base)


def _binding(repo="jeffkit/recursive"):
    return RepoBinding(repo=repo, profile="p", cwd="/tmp/nonexistent-clone")


def _res(number=5):
    return Resource(kind="issue", number=number, title="t", body="正文", state="open",
                    labels=[], author="bob", created_at="", updated_at="",
                    status="inbox", actor_type="human")


@pytest.fixture()
def art(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".issue-keeper" / "pipeline" / "recursive-5"
    d.mkdir(parents=True)
    return d


def _wire(monkeypatch, client):
    monkeypatch.setattr(ce, "client_from_config", lambda cfg: client)
    posted = []
    monkeypatch.setattr(K, "_gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    return posted


class TestDispatchConsole:
    def test_dispatch_writes_anchors_and_locks(self, art, monkeypatch):
        client = FakeClient()
        _wire(monkeypatch, client)
        cfg = _cfg()
        pc = cfg.pipeline_repo_cfg("jeffkit/recursive")
        it = ItemState()
        out = K._dispatch_console_execution(cfg, _binding(), _res(), it, "l",
                                            pc, art, art / "00-issue.md")
        assert out["status"] == "dispatched"
        assert client.started[0][0] == "self-improve-v2"
        params = client.started[0][1]
        assert params["repo"] == "/tmp/nonexistent-clone"
        assert "#5" in params["goal"]
        rec = json.loads((art / K.CONSOLE_EXEC_RECORD).read_text())
        assert rec["execution_id"] == "exec-123" and rec["retry_count"] == 0
        assert (art / "v2-goal.md").exists()
        dispatch = json.loads((art / "dispatch.json").read_text())
        assert dispatch["worktree_dir"].endswith(".worktrees/issue-5")
        assert (art / K.PIPELINE_LOCK_NAME).read_text() == K.CONSOLE_LOCK_SENTINEL
        assert it.in_flight_since is not None

    def test_dispatch_wraps_gate_cmds_in_bash_c(self, art, monkeypatch):
        """门命令必须显式 bash -c 包装（plaita#28「gates/tests 失败」根因）。

        flow 的 GATE 节点对单字符串命令按 argv 执行、不经 shell：不包装则
        `cd X && Y` 静默假绿（cd 吞掉剩余参数返回 0）、`pytest … && pytest …`
        参数错乱报 usage error。本地 gate_runner 对同一份命令是 bash -c 语义，
        两条路径必须对齐。"""
        client = FakeClient()
        _wire(monkeypatch, client)
        cfg = _cfg()
        pc = cfg.pipeline_repo_cfg("jeffkit/recursive")
        pc.gates = [GateSpec(name="tests",
                             command="pytest -q && pytest tests/e2e -q",
                             timeout_secs=1800)]
        pc.setup_command = "pnpm install --frozen-lockfile"
        it = ItemState()
        out = K._dispatch_console_execution(cfg, _binding(), _res(), it, "l",
                                            pc, art, art / "00-issue.md")
        assert out["status"] == "dispatched"
        params = client.started[0][1]
        gates = params["gates"]
        assert gates[0]["name"] == "tests"
        assert gates[0]["cmd"].startswith("bash -c ")
        assert "pytest -q && pytest tests/e2e -q" in gates[0]["cmd"]
        # per-repo 预算透传（flow 侧 fmt/lint/test 三槽位读注入值，2026-10-07）
        assert gates[0]["timeout_secs"] == 1800
        # setup 透传（flow preflight 在 worktree 建立后执行）
        assert params["setup_command"] == "pnpm install --frozen-lockfile"

    def test_dispatch_failure_is_engine_error_without_record(self, art, monkeypatch):
        _wire(monkeypatch, FakeClient(fail_start="boom"))
        cfg = _cfg()
        pc = cfg.pipeline_repo_cfg("jeffkit/recursive")
        it = ItemState()
        out = K._dispatch_console_execution(cfg, _binding(), _res(), it, "l",
                                            pc, art, art / "00-issue.md")
        assert out["status"] == "engine_error"
        assert not (art / K.CONSOLE_EXEC_RECORD).exists()

    def test_in_flight_detected_via_record(self, art):
        (art / K.CONSOLE_EXEC_RECORD).write_text(json.dumps({"execution_id": "e"}))
        assert K._pipeline_in_flight(art) == -1


class TestReapConsole:
    def _reap(self, art, monkeypatch, client, dispatched_ago=100.0):
        cfg = _cfg()
        it = ItemState()
        it.in_flight_since = time.time() - dispatched_ago
        rec_path = art / K.CONSOLE_EXEC_RECORD
        if not rec_path.exists():  # 二轮场景保留首轮写的 retry_count
            rec_path.write_text(json.dumps(
                {"execution_id": "exec-123", "flow_id": "self-improve-v2",
                 "retry_count": 0}))
        posted = _wire(monkeypatch, client)  # 先装 mock 再调
        row = K._reap_console_execution(
            cfg, _binding(), it, "5", "l", art,
            json.loads(rec_path.read_text()), time.time())
        return cfg, it, row, posted

    def test_running_fresh_stays_in_flight(self, art, monkeypatch):
        fresh = {"status": "running",
                 "last_update_time": datetime.now().isoformat()}
        cfg, it, row, posted = self._reap(art, monkeypatch, FakeClient(fresh))
        assert row is None
        assert (art / K.CONSOLE_EXEC_RECORD).exists()

    def test_running_zombie_cancels_and_lands_engine_error(self, art, monkeypatch):
        old = {"status": "running",
               "last_update_time": (datetime.now() - timedelta(seconds=99999)).isoformat()}
        client = FakeClient(old)
        cfg, it, row, posted = self._reap(art, monkeypatch, client)
        assert client.cancelled == ["exec-123"]
        assert row["status"] == "engine_error" and "zombie" in row["error"]
        assert not (art / K.CONSOLE_EXEC_RECORD).exists()
        ledger = (art.parent / "runs.jsonl").read_text().strip().splitlines()
        assert json.loads(ledger[-1])["status"] == "engine_error"

    def test_error_resumes_retry_once_then_exhausts(self, art, monkeypatch):
        err = {"status": "error", "error": {"message": "AgentRunError"}}
        client = FakeClient(err)
        cfg, it, row, posted = self._reap(art, monkeypatch, client)
        assert row is None and client.resumed == [("exec-123", "retry")]
        rec = json.loads((art / K.CONSOLE_EXEC_RECORD).read_text())
        assert rec["retry_count"] == 1
        # 第二轮 error：retry 额度已用尽 → engine_error 行
        cfg2, it2, row2, _ = self._reap(art, monkeypatch, client)
        assert row2["status"] == "engine_error"
        assert not (art / K.CONSOLE_EXEC_RECORD).exists()

    def test_completed_posts_closing_and_lands_row(self, art, monkeypatch):
        done = {"status": "completed",
                "context": {"$NODE": {"verdict": {"verdict": "committed",
                                                  "via": "git-publish"}}}}
        cfg, it, row, posted = self._reap(art, monkeypatch, FakeClient(done))
        assert row["status"] == "done" and row["comment_posted"] is True
        assert len(posted) == 1 and "done" in posted[0]
        ledger = (art.parent / "runs.jsonl").read_text().strip().splitlines()
        last = json.loads(ledger[-1])
        assert last["status"] == "done" and last["comment_posted"] is True
        assert last["flow_source"] == "v2-console"

    def test_console_unreachable_skips_round(self, art, monkeypatch):
        import urllib.error

        class DownClient:
            def get_execution(self, eid):
                raise ce.ConsoleExecUnavailable("down")

        _wire(monkeypatch, DownClient())
        cfg = _cfg()
        it = ItemState()
        (art / K.CONSOLE_EXEC_RECORD).write_text(json.dumps({"execution_id": "e"}))
        row = K._reap_console_execution(cfg, _binding(), it, "5", "l", art,
                                        {"execution_id": "e"}, time.time())
        assert row is None
        assert (art / K.CONSOLE_EXEC_RECORD).exists()  # 在途锚保留
