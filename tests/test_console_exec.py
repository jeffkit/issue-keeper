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


class TestWorkerAlive:
    """D-0027 修正（#20）：判活看在跑节点/带 TTL 的租约/心跳，不看无 TTL 持久键。

    实测执行键形态：plaita:execution:{id} TTL=-1（永久）——「键存在」不代表
    worker 活着，旧「0 租约」判据对它永不成立。
    判死纪律（reviewer blocking fix）：节点史存在≠死——最新 ended_at 停滞超
    node_stale_secs 才算死；节点间空档/末节点刚结束等秒级窗口必须判 None，
    否则 reaper 误杀健康 run（plaita#18 重复执行）。
    """

    def _nodes(self, **kw):
        now = datetime.now()
        ended = now - timedelta(**kw)
        return {"_n17": {"started_at": (ended - timedelta(minutes=10)).isoformat(),
                         "ended_at": ended.isoformat()}}

    def test_no_signals_is_unknown(self):
        """无节点史无心跳（刚起步）→ None：不下结论，交给超时兜底。"""
        assert ce.worker_alive({"status": "running"}) is None

    def test_running_node_is_alive(self):
        d = {"status": "running",
             "node_timings": {"impl": {"started_at": datetime.now().isoformat()}}}
        assert ce.worker_alive(d) is True

    def test_node_just_ended_is_not_dead(self):
        """reviewer blocking case：末节点 30s 前结束（节点间空档/终态落盘窗口）
        → None 不下结论。修复前此处返回 False → zombie 误杀健康 run。"""
        d = {"status": "running", "node_timings": self._nodes(seconds=30)}
        assert ce.worker_alive(d) is None

    def test_all_nodes_ended_and_stale_is_dead(self):
        """#28 形态：11 个节点全停在失败时刻、无租约无心跳 → 确证死亡。"""
        d = {"status": "running",
             "node_timings": self._nodes(hours=3)}
        assert ce.worker_alive(d) is False

    def test_staleness_uses_newest_ended_at(self):
        """判死看**最新** ended_at：末节点刚结束而早节点老化 → 不是死。"""
        d = {"status": "running",
             "node_timings": {"a": self._nodes(hours=3)["_n17"],
                              "b": {"started_at": (datetime.now()
                                     - timedelta(minutes=5)).isoformat(),
                                    "ended_at": (datetime.now()
                                     - timedelta(seconds=30)).isoformat()}}}
        assert ce.worker_alive(d) is None

    def test_inprogress_node_too_old_is_unknown_not_dead(self):
        """started 超 1 天仍未 ended 的节点不作活证据，但也不据此判死——
        观测残缺（节点重试后 started_at 是否刷新未实测）不下结论，交
        last_update_time 兜底（本例它 ≥ 节点年龄，兜底线必然接得住）。"""
        started = (datetime.now() - timedelta(days=2)).isoformat()
        d = {"status": "running",
             "node_timings": {"impl": {"started_at": started}}}
        assert ce.worker_alive(d) is None
        d["last_update_time"] = started
        assert ce.zombie(d, 7200) is True

    def test_fresh_heartbeat_is_alive(self):
        d = {"status": "running",
             "worker_heartbeat": (datetime.now() - timedelta(seconds=10)).isoformat()}
        assert ce.worker_alive(d) is True

    def test_stale_heartbeat_is_not_alive(self):
        d = {"status": "running",
             "worker_heartbeat": (datetime.now() - timedelta(minutes=10)).isoformat()}
        assert ce.worker_alive(d) is False

    def test_unexpired_lease_is_alive(self):
        d = {"status": "running",
             "lease": {"expires_at": (datetime.now() + timedelta(seconds=30)).isoformat()}}
        assert ce.worker_alive(d) is True

    def test_expired_lease_is_not_alive(self):
        d = {"status": "running",
             "lease": {"expires_at": (datetime.now() - timedelta(seconds=30)).isoformat()}}
        assert ce.worker_alive(d) is False

    def test_running_node_beats_expired_lease(self):
        """reviewer secondary fix：在跑节点是最强活证据，优先于租约字段——
        过期/持久化过期的租约时间戳不得否决正在跑的节点（① 先于 ②）。"""
        d = {"status": "running",
             "lease": {"expires_at": (datetime.now() - timedelta(seconds=30)).isoformat()},
             "node_timings": {"impl": {"started_at": datetime.now().isoformat()}}}
        assert ce.worker_alive(d) is True

    def test_terminal_status_never_alive(self):
        assert ce.worker_alive({"status": "completed"}) is False


class TestZombieAliveMerge:
    """zombie 与 worker_alive 合流：活性确证死亡即判死，不等 last_update_time 老化。"""

    def test_dead_worker_kills_regardless_of_last_update(self):
        """#20 核心：执行键存在（TTL=-1）+ 无心跳 + 节点史停滞超阈 → 收尸，
        即使 last_update_time 被 resume 刷新过（旧判据在此永不成立）。"""
        d = {"status": "running",
             "last_update_time": datetime.now().isoformat(),   # 新鲜——旧判据看不到死
             "node_timings": {"a": {"started_at": (datetime.now()
                                       - timedelta(hours=3)).isoformat(),
                                    "ended_at": (datetime.now()
                                       - timedelta(hours=2, minutes=50)).isoformat()}}}
        assert ce.zombie(d, 7200) is True

    def test_alive_worker_not_killed_by_old_last_update(self):
        """长 impl 节点在跑（无 ended）：last_update_time 老化是正常形态，不判死。"""
        d = {"status": "running",
             "last_update_time": (datetime.now() - timedelta(hours=3)).isoformat(),
             "node_timings": {"impl": {"started_at": (datetime.now()
                                        - timedelta(hours=2)).isoformat()}}}
        assert ce.zombie(d, 7200) is False

    def test_recent_node_end_falls_back_to_last_update(self):
        """reviewer blocking case 全链路：末节点 30s 前结束 → worker_alive=None，
        zombie 退回 last_update_time 兜底（新鲜 → 不判死）。修复前这里误杀。"""
        d = {"status": "running",
             "last_update_time": (datetime.now() - timedelta(minutes=5)).isoformat(),
             "node_timings": {"a": {"started_at": (datetime.now()
                                       - timedelta(hours=1)).isoformat(),
                                    "ended_at": (datetime.now()
                                       - timedelta(seconds=30)).isoformat()}}}
        assert ce.zombie(d, 7200) is False

    def test_stale_node_end_kills_even_with_fresh_last_update(self):
        """停滞超阈（默认 1800s）才是确证死亡：last_update 新鲜拦不住。"""
        d = {"status": "running",
             "last_update_time": datetime.now().isoformat(),
             "node_timings": {"a": {"started_at": (datetime.now()
                                       - timedelta(hours=3)).isoformat(),
                                    "ended_at": (datetime.now()
                                       - timedelta(minutes=31)).isoformat()}}}
        assert ce.zombie(d, 7200) is True


class TestInflightOverrun:
    """兜底收尸线（#20 验收 2）：在途总时长越预算即判死，不要求「无租约」前提。"""

    def _detail(self, age_secs, status="running"):
        return {"status": status,
                "start_time": (datetime.now() - timedelta(seconds=age_secs)).isoformat()}

    def test_over_budget_kills(self):
        assert ce.inflight_overrun(self._detail(4 * 3600), 3 * 3600) is True

    def test_within_budget_alive(self):
        assert ce.inflight_overrun(self._detail(3600), 3 * 3600) is False

    def test_zero_budget_disables(self):
        assert ce.inflight_overrun(self._detail(99 * 3600), 0) is False

    def test_terminal_never_overrun(self):
        assert ce.inflight_overrun(self._detail(99 * 3600, status="completed"),
                                   3 * 3600) is False

    def test_no_start_time_no_verdict(self):
        assert ce.inflight_overrun({"status": "running"}, 3 * 3600) is False


class TestRecordQueued:
    """plaita#18：「已派发未消费」窗口（GET 404）判排队中，非故障。"""

    def _now(self):
        return datetime(2026, 10, 7, 12, 0, 0).timestamp()

    def test_fresh_record_is_queued(self):
        now = self._now()
        crec = {"dispatched_at": (datetime.fromtimestamp(now)
                                  - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S%z")}
        assert ce.record_queued(crec, 1800, now=now) is True

    def test_old_record_is_not_queued(self):
        now = self._now()
        crec = {"dispatched_at": (datetime.fromtimestamp(now)
                                  - timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%S%z")}
        assert ce.record_queued(crec, 1800, now=now) is False

    def test_missing_dispatched_at_falls_back_to_inflight_since(self):
        now = self._now()
        assert ce.record_queued({}, 1800, inflight_since=now - 60, now=now) is True
        assert ce.record_queued({}, 1800, inflight_since=now - 3600, now=now) is False

    def test_no_clock_reference_is_not_queued(self):
        assert ce.record_queued({}, 1800, now=self._now()) is False


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
    def __init__(self, detail=None, fail_start=None, not_found_get=False,
                 fail_resume=False):
        self.detail = detail or {}
        self.fail_start = fail_start
        self.not_found_get = not_found_get
        self.fail_resume = fail_resume
        self.started = []
        self.resumed = []
        self.cancelled = []

    def start_execution(self, flow_id, params):
        if self.fail_start:
            raise ce.ConsoleExecError(self.fail_start)
        self.started.append((flow_id, params))
        return "exec-123"

    def get_execution(self, eid):
        if self.not_found_get:
            raise ce.ConsoleExecNotFound(f"/api/executions/{eid}: 404")
        return self.detail

    def resume(self, eid, resume_type, data=None):
        self.resumed.append((eid, resume_type))
        if self.fail_resume:
            raise ce.ConsoleExecNotFound(f"/api/executions/{eid}/resume: 404")
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
        # 完整 gate spec 透传（flow v1.0.5 主路径：落盘后调同一个 gate_runner，
        # N 道门/独立预算/paths/autofix 与本地路径逐字段等价）
        spec = json.loads(params["gates_spec"])
        assert spec["base"] == "main"
        assert spec["gates"][0]["name"] == "tests"
        assert spec["gates"][0]["command"] == "pytest -q && pytest tests/e2e -q"  # 原文，不包 bash -c
        assert params["gate_runner"].endswith("flows/gates/gate_runner.py")
        assert params["gate_timeout_secs"] > 0

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

    def test_running_no_signal_stale_last_update_stays_in_flight(self, art, monkeypatch):
        """刚起步（无节点史无心跳）：worker_alive 无判据，旧 last_update 兜底
        未到阈 → 仍在途。"""
        d = {"status": "running",
             "last_update_time": (datetime.now() - timedelta(seconds=3600)).isoformat()}
        cfg, it, row, posted = self._reap(art, monkeypatch, FakeClient(d))
        assert row is None

    def test_issue20_accreditation_exec_key_exists_no_heartbeat_4h(self, art, monkeypatch):
        """#20 验收单测：执行键存在（TTL=-1 持久键）+ 无租约无心跳 +
        在途 4h → 收尸发生（修复前：旧判据要求「0 租约」+ last_update 老化，
        而租约键无 TTL ⇒ 永不成立，229 分钟无人收尸）。"""
        stalled = {"status": "running",
                   "start_time": (datetime.now() - timedelta(hours=4)).isoformat(),
                   "last_update_time": (datetime.now() - timedelta(hours=4)).isoformat(),
                   "node_timings": {
                       f"_n{i}": {"started_at": (datetime.now()
                                                  - timedelta(hours=3)).isoformat(),
                                  "ended_at": (datetime.now()
                                               - timedelta(hours=2, minutes=50)
                                               ).isoformat()}
                       for i in range(11)}}
        client = FakeClient(stalled)
        cfg, it, row, posted = self._reap(art, monkeypatch, client,
                                          dispatched_ago=4 * 3600)
        assert client.cancelled == ["exec-123"]
        assert row["status"] == "engine_error"
        assert "超时" in row["error"] or "zombie" in row["error"]
        assert not (art / K.CONSOLE_EXEC_RECORD).exists()   # 在途锚已清 → 交回重派

    def test_overrun_budget_reaps_even_with_fresh_last_update(self, art, monkeypatch):
        """兜底线（验收 2）：在途越预算即判死，不要求无租约前提，
        last_update_time 被 resume 刷新也拦不住。

        取值随默认预算调整（2026-10-11：10800 → 24300，见 config.py 该字段
        的长注释）。此处**从配置读默认值**而非写死小时数——否则每次调预算都要
        手改测试，且写死会掩盖「测试意图 = 越线即收」这一契约。
        """
        budget_h = K.Config().console_inflight_budget_secs / 3600.0
        over_h = budget_h + 1.0                       # 明确越线 1h
        d = {"status": "running",
             "start_time": (datetime.now() - timedelta(hours=over_h)).isoformat(),
             "last_update_time": datetime.now().isoformat()}   # 新鲜
        client = FakeClient(d)
        cfg, it, row, posted = self._reap(art, monkeypatch, client,
                                          dispatched_ago=int(over_h * 3600))
        assert client.cancelled == ["exec-123"]
        assert row["status"] == "engine_error" and "超时" in row["error"]

    def test_budget_zero_disables_overrun_line(self, art, monkeypatch):
        """兜底线关掉（0）后，仅剩 zombie 线：无节点史 + last_update 新鲜 → 不判死。"""
        d = {"status": "running",
             "start_time": (datetime.now() - timedelta(hours=99)).isoformat(),
             "last_update_time": datetime.now().isoformat()}
        client = FakeClient(d)
        cfg, it, row, _ = self._reap(
            art, monkeypatch, client, dispatched_ago=99 * 3600)
        cfg.console_inflight_budget_secs = 0
        row2 = K._reap_console_execution(
            cfg, _binding(), it, "5", "l", art,
            {"execution_id": "exec-123", "retry_count": 0}, time.time())
        assert row2 is None                       # 仅剩 zombie 线，未触发
        assert client.cancelled == ["exec-123"]   # 第一轮（默认预算）已收尸

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

    def test_retry_later_lands_row_without_closing_comment(self, art, monkeypatch):
        """D-0025：retry-later 是环境性 deferral（console_exec.py 语义表）——
        台账行照落，但不回评（否则磁盘守卫风暴会给 issue 刷无意义收尾评论）。"""
        defer = {"status": "completed",
                 "context": {"$NODE": {"verdict": {"verdict": "retry-later",
                                                   "stage": "preflight",
                                                   "why": "disk 15.0GiB < min"}}}}
        cfg, it, row, posted = self._reap(art, monkeypatch, FakeClient(defer))
        assert row["status"] == "retry-later" and row["comment_posted"] is False
        assert posted == []
        ledger = (art.parent / "runs.jsonl").read_text().strip().splitlines()
        last = json.loads(ledger[-1])
        assert last["status"] == "retry-later" and last["comment_posted"] is False

    def test_failed_preserved_still_posts_closing(self, art, monkeypatch):
        """对照面：failed-preserved（内容性失败）仍出收尾回评——D-0025 只豁免 retry-later。"""
        bad = {"status": "completed",
               "context": {"$NODE": {"verdict": {"verdict": "failed-preserved",
                                                 "stage": "review",
                                                 "why": "review did not pass"}}}}
        cfg, it, row, posted = self._reap(art, monkeypatch, FakeClient(bad))
        assert row["status"] == "failed" and row["comment_posted"] is True
        assert len(posted) == 1 and "failed" in posted[0]

    def _write_rec(self, art, age_secs):
        (art / K.CONSOLE_EXEC_RECORD).write_text(json.dumps({
            "execution_id": "exec-123", "flow_id": "self-improve-v2",
            "retry_count": 0,
            "dispatched_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - age_secs))}))

    def test_404_within_grace_is_queued_not_engine_error(self, art, monkeypatch):
        """plaita#18：派发后记录未落（worker 未消费）→ 404 判排队中，不重派。

        时序：console POST 只入队 Redis，执行记录由 worker 消费时才首次落盘；
        背压排队下该窗口 >1 个 keeper 周期。误判 engine_error 会 re-dispatch
        同一 issue → 重复执行。
        """
        self._write_rec(art, age_secs=60)
        client = FakeClient(not_found_get=True)
        cfg, it, row, posted = self._reap(art, monkeypatch, client)
        assert row is None                       # 仍在途
        assert client.resumed == []              # 不 resume
        assert posted == []                      # 不回评
        assert (art / K.CONSOLE_EXEC_RECORD).exists()      # 在途锚保留
        assert not (art.parent / "runs.jsonl").exists()    # 不落台账行

    def test_404_past_grace_keeps_self_heal_path(self, art, monkeypatch):
        """记录年龄超宽限期仍 404 → 既有自愈路径（resume → 失败转 engine_error）。"""
        self._write_rec(art, age_secs=9999)   # in_flight_since 仍是新鲜的 100s
        client = FakeClient(not_found_get=True, fail_resume=True)
        cfg, it, row, posted = self._reap(art, monkeypatch, client)
        assert client.resumed == [("exec-123", "retry")]
        assert row["status"] == "engine_error" and "404" in row["error"]
        assert not (art / K.CONSOLE_EXEC_RECORD).exists()
        ledger = (art.parent / "runs.jsonl").read_text().strip().splitlines()
        assert json.loads(ledger[-1])["status"] == "engine_error"

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
