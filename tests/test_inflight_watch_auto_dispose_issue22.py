"""#22 回归——inflight-watch 第二阶梯：双 120 死档的机械白名单处置。

背景（2026-10-09）：单日 5 次 cancel 全是同一 signature（sbx 通道条件假分支悬死，
引擎缺口 jeffkit/plaita#53）；现行阶梯到「resume 无效」就断了，靠值守人工
cancel+reopen——值守轮次 30min+，夜间无人时卡死 run 空跑 2h+ 无人收。

本文件钉死的行为：
- 触发 = running + 末节点结束 >120min **且** 进度龄 >120min（双交集）+ resume 额度已用；
  活跃 impl（进度龄 <120min）不触发；无进度信号不触发（证明不了状态两小时没刷新）；
- 动作顺序 = salvage 快照（cancel 前）→ cancel → reopen → 杀沙箱实例；cancel 失败即止；
- 熔断 = 同 issue 24h 内已自动 cancel ≥2 次 → 不动作、改递值守工单；
- 留痕 = 账本 cancelled 记录 + duty state 的 actions（cancel_execution / reopen_issue,
  level=authorized）+ rounds.log 一行。

驱动方式同 test_sandbox_watch_issue23：从编译产物 `flows/inflight-watch.flow.json`
取节点 code 直接 exec（= 运行期真正执行的定义），ssh / console / e2b 全部打桩。
"""
import io
import json
import pathlib
import sys
import time
import types

import pytest

HERE = pathlib.Path(__file__).resolve().parent
FLOW_JSON = HERE.parent / "flows" / "inflight-watch.flow.json"

EID = "dbd5e8193aa94bb3"
LABEL = "recursive#145"
SANDBOX_ID = "amuvsy4n2jipoktbkbssnovo23bfll2lshpsq2pf"


def _node(nid):
    ir = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
    spec = next(n for n in ir["nodes"] if n.get("id") == nid)
    ns: dict = {}
    exec(spec["code"], ns)  # code 节点：取 run(input) 直接驱动
    return ns["run"]


@pytest.fixture()
def triage():
    return _node("triage")


@pytest.fixture()
def act():
    return _node("act")


@pytest.fixture()
def finish():
    return _node("finish")


def _row(**kw):
    """一行在途事实（facts 节点产出的形态）。默认=双 120 命中。"""
    base = {
        "repo": "recursive", "num": "145", "in_flight_min": 330, "exec_id": EID,
        "flow_id": "self-improve-v2-sbx", "retry_after": None, "retry_pending": False,
        "repo_full": "jeffkit/recursive",
        "run_dir": "/home/ubuntu/.issue-keeper/pipeline/recursive-145",
        "worktree_dir": "/home/ubuntu/projects/infra4agent/recursive/.worktrees/issue-145",
        "exec_status": "running", "nodes": 11, "has_node_ts": True,
        "in_progress_nodes": 0, "last_node_ended_age_min": 150, "progress_age_min": 150,
    }
    base.update(kw)
    return base


@pytest.fixture()
def ledgers(tmp_path):
    """账本读写小工具：盘上就是 act 的 inflight-resume.json。"""
    path = tmp_path / "inflight-resume.json"

    def seed(resumed=None, cancelled=None):
        path.write_text(json.dumps({"resumed": resumed or {}, "cancelled": cancelled or {}}),
                        encoding="utf-8")

    def read():
        return json.loads(path.read_text(encoding="utf-8"))

    return types.SimpleNamespace(dir=str(tmp_path), path=path, seed=seed, read=read)


@pytest.fixture()
def trace():
    """动作次序（salvage/cancel/reopen）——顺序本身就是契约。"""
    return []


class _FakeHttp:
    """console API 打桩：记下每个请求，按 URL 关键字可注入失败。"""

    def __init__(self, trace):
        self.calls = []
        self.trace = trace
        self.fail_on = ()

    def urlopen(self, req, timeout=None):
        url = getattr(req, "full_url", str(req))
        self.calls.append(url)
        self.trace.append("cancel" if "/cancel" in url else "resume")
        if any(k in url for k in self.fail_on):
            raise OSError("console 不可达（打桩）")
        return io.BytesIO(json.dumps({"status": "ok"}).encode())

    def kind(self, needle):
        return [c for c in self.calls if needle in c]


@pytest.fixture()
def http(monkeypatch, trace):
    fake = _FakeHttp(trace)
    monkeypatch.setattr("urllib.request.urlopen", fake.urlopen)
    return fake


class _FakeSsh:
    """ssh 打桩：按送达的脚本内容认领是 salvage 还是 reopen。"""

    def __init__(self, trace):
        self.scripts = []
        self.trace = trace
        self.reopen_rc = 0

    def __call__(self, cmd, **kw):
        src = kw.get("input") or ""
        self.scripts.append(src)
        if "salvage-snapshot" in src:
            self.trace.append("salvage")
            out = json.dumps({"ok": True, "path": "/home/ubuntu/.issue-keeper/pipeline/"
                                                "recursive-145/salvage-snapshot.patch",
                              "bytes": 1024})
        elif "issue_keeper" in src:
            self.trace.append("reopen")
            out = json.dumps({"rc": self.reopen_rc, "out": "changed=[145]", "err": ""})
        else:
            out = "{}"
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


@pytest.fixture()
def ssh(monkeypatch, trace):
    fake = _FakeSsh(trace)
    monkeypatch.setattr("subprocess.run", fake)
    return fake


class _FakeSandbox:
    killed = []

    @classmethod
    def kill(cls, sid):
        cls.killed.append(sid)
        return True


@pytest.fixture()
def sandbox(monkeypatch):
    mod = types.ModuleType("e2b")
    mod.Sandbox = _FakeSandbox
    monkeypatch.setitem(sys.modules, "e2b", mod)
    _FakeSandbox.killed = []
    return _FakeSandbox


def _dispose_inputs(rows, instances=None, duty_dir=None, **kw):
    """act 的两个阶梯入参（第一阶梯的 stalled 通常与死档列表同批）。"""
    out = {
        "stalled": rows,
        "hard_dead": rows,
        "instances": instances if instances is not None else [{"id": SANDBOX_ID, "exec": EID,
                                                              "short": SANDBOX_ID[:14]}],
        "duty_dir": duty_dir,
    }
    out.update(kw)
    return out


class TestTriageDouble120:
    """验收 1/2 的判据侧：双 120 交集进死档，活跃 impl 与模糊形态都不进。"""

    def test_double_120_is_hard_dead(self, triage):
        out = triage({"rows": [_row()], "instances": []})
        assert [r["num"] for r in out["hard_dead"]] == ["145"]
        assert any("双 120 命中" in f["summary"] for f in out["findings"])
        assert "双120死档 1" in out["report"]

    def test_active_impl_not_hard_dead(self, triage):
        # 验收 2：末节点结束已久（200min）但进度龄 30min（impl 在写状态）→ 不触发
        out = triage({"rows": [_row(last_node_ended_age_min=200, progress_age_min=30)],
                      "instances": []})
        assert out["hard_dead"] == []

    def test_long_impl_with_open_node_not_hard_dead(self, triage):
        # 有开节点：payload 不填「末节点结束龄」——健康长 impl 天然进不来
        out = triage({"rows": [_row(in_progress_nodes=1, last_node_ended_age_min=None,
                                    progress_age_min=140)], "instances": []})
        assert out["hard_dead"] == []

    def test_no_progress_signal_not_hard_dead(self, triage):
        # 无进度信号 = 证明不了「两小时没刷新」，有损档不赌
        out = triage({"rows": [_row(progress_age_min=None)], "instances": []})
        assert out["hard_dead"] == []

    def test_threshold_floor_is_120(self, triage):
        # INPUT schema 可能把 dead_min 调小（#40 的教训：`or` 兜底被 schema 盖掉）
        out = triage({"rows": [_row(last_node_ended_age_min=90, progress_age_min=90)],
                      "instances": [], "dead_min": 10})
        assert out["hard_dead"] == []

    def test_stall_min_raised_does_not_gate_out_dead(self, triage):
        """死档入口不跟 stall_min 走：它被调大（SKILL.md 记过 30→130 的掩盖先例）
        也不能把双 120 的死档挡在门外。"""
        out = triage({"rows": [_row()], "instances": [], "stall_min": 200})
        assert [r["num"] for r in out["hard_dead"]] == ["145"]

    def test_exactly_at_line_not_dead(self, triage):
        out = triage({"rows": [_row(last_node_ended_age_min=120, progress_age_min=120)],
                      "instances": []})
        assert out["hard_dead"] == []

    def test_terminal_exec_never_dead(self, triage):
        out = triage({"rows": [_row(exec_status="cancelled", last_node_ended_age_min=200,
                                    progress_age_min=200)], "instances": []})
        assert out["hard_dead"] == []
        assert len(out["terminal_lag"]) == 1  # 收尸滞后语义不变


class TestAutoDispose:
    """验收 1：resume 额度已用的双 120 → 快照 + cancel + reopen + 杀沙箱 + 账本留痕。"""

    def test_disposed_after_resume_quota_used(self, act, ledgers, ssh, http, sandbox, trace):
        ledgers.seed(resumed={EID: {"at": "2026-10-09T01:00:00", "label": LABEL}})
        out = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert trace == ["salvage", "cancel", "reopen"]
        assert out["cancelled"] == [LABEL]
        assert out["breaker"] == []
        assert len(http.kind("/executions/%s/cancel" % EID)) == 1
        assert sandbox.killed == [SANDBOX_ID]
        doc = ledgers.read()
        assert list(doc["cancelled"]) == [LABEL]
        rec = doc["cancelled"][LABEL][0]
        assert rec["exec"] == EID and rec["salvage"]["ok"] is True
        assert rec["reopen"]["rc"] == 0 and rec["sandbox"] is True
        assert doc["resumed"][EID]["label"] == LABEL  # 第一阶梯账目不被覆盖

    def test_salvage_before_cancel(self, act, ledgers, ssh, http, sandbox, trace):
        """快照必须先于 cancel（cancel 后 worktree 可能被收尾清掉）。"""
        ledgers.seed(resumed={EID: {"at": "x"}})
        act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert trace.index("salvage") < trace.index("cancel")

    def test_resume_quota_unused_only_resumes(self, act, ledgers, ssh, http, sandbox, trace):
        """额度未用：本轮只 resume（救命稻草），不在同一轮直接 cancel。"""
        out = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert trace == ["resume"]
        assert out["resumed"] == [LABEL]
        assert out["cancelled"] == []
        assert ssh.scripts == [] and sandbox.killed == []
        assert EID in ledgers.read()["resumed"]

    def test_dead_row_not_in_stalled_still_gets_resume_first(self, act, ledgers, ssh, http,
                                                             sandbox, trace):
        """hard_min 被调大时死档可能不在 stalled 里——第一阶梯按并集补 resume。"""
        out = act({"stalled": [], "hard_dead": [_row()], "instances": [], "duty_dir": ledgers.dir})
        assert out["resumed"] == [LABEL] and out["cancelled"] == []
        assert trace == ["resume"]

    def test_second_round_disposes_after_first_round_resume(self, act, ledgers, ssh, http,
                                                            sandbox, trace):
        """跨轮语义：第 N 轮 resume，第 N+1 轮（额度已落账）才 cancel。"""
        round1 = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert round1["resumed"] == [LABEL] and round1["cancelled"] == []
        trace.clear()
        round2 = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert round2["cancelled"] == [LABEL]
        assert round2["resumed"] == []
        assert trace == ["salvage", "cancel", "reopen"]

    def test_cancel_failure_stops_before_reopen(self, act, ledgers, ssh, http, sandbox, trace):
        """cancel 失败即止：run 可能还在跑，reopen 会造重复 run。"""
        ledgers.seed(resumed={EID: {"at": "x"}})
        http.fail_on = ("/cancel",)
        out = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert out["cancelled"] == []
        assert trace == ["salvage", "cancel"]     # 没 reopen
        assert sandbox.killed == []
        assert ledgers.read()["cancelled"] == {}
        crit = [f for f in out["findings"] if f["severity"] == "critical"]
        assert crit and "cancel 失败" in crit[0]["summary"]

    def test_reopen_failure_is_critical(self, act, ledgers, ssh, http, sandbox, trace):
        ledgers.seed(resumed={EID: {"at": "x"}})
        ssh.reopen_rc = 2
        out = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert out["cancelled"] == [LABEL]        # cancel 已发生，账要如实记
        assert out["reopen_failed"] == [LABEL]    # 失败要能被 finish 看见（复评阻断项）
        crit = [f for f in out["findings"] if f["severity"] == "critical"]
        assert crit and "reopen 未成功" in crit[0]["summary"]

    def test_dead_row_resume_failure_uses_up_quota(self, act, ledgers, ssh, http, sandbox,
                                                   trace):
        """resume 抛错（console 报错）对死档也要落账＝额度已用。

        否则死档永远停在「额度未用」：每轮 resume 都抛错 → 既不进机械档也不留痕，
        「无人收尸」原地复发（2026-10-10 复评观察 3）。
        """
        http.fail_on = ("/resume",)
        round1 = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert round1["cancelled"] == [] and round1["resumed"] == []
        assert EID in ledgers.read()["resumed"]     # 尝试过 = 额度已用
        http.fail_on = ()
        round2 = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert round2["cancelled"] == [LABEL]       # 下一轮进机械档

    def test_stalled_resume_failure_is_retried_next_round(self, act, ledgers, ssh, http,
                                                          sandbox, trace):
        """非死档的 stalled 不在此列：救命稻草没递出去就下轮重试，不占额度。"""
        http.fail_on = ("/resume",)
        out = act({"stalled": [_row(last_node_ended_age_min=60, progress_age_min=60)],
                   "hard_dead": [], "instances": [], "duty_dir": ledgers.dir})
        assert out["resumed"] == [] and out["cancelled"] == []
        assert ledgers.read()["resumed"] == {}

    def test_dryrun_does_nothing(self, act, ledgers, ssh, http, sandbox, trace):
        ledgers.seed(resumed={EID: {"at": "x"}})
        out = act(_dispose_inputs([_row()], duty_dir=ledgers.dir, dryrun=True))
        assert out["why"] == "dryrun" and out["cancelled"] == []
        assert trace == [] and ssh.scripts == [] and sandbox.killed == []


class TestBreaker:
    """验收 3：同 issue 24h 内已 cancel 2 次 → 不再自动处置，改递值守工单。"""

    @staticmethod
    def _hist(now, n):
        return [{"at": now - 3600 * (i + 1), "exec": "old%d" % i} for i in range(n)]

    def test_third_stall_breaks_circuit(self, act, ledgers, ssh, http, sandbox, trace):
        ledgers.seed(resumed={EID: {"at": "x"}},
                     cancelled={LABEL: self._hist(time.time(), 2)})
        out = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert out["breaker"] == [LABEL]
        assert out["cancelled"] == []
        assert trace == [] and sandbox.killed == [] and ssh.scripts == []
        crit = [f for f in out["findings"] if f["severity"] == "critical"]
        assert crit and "熔断" in crit[0]["summary"]

    def test_window_expired_allows_dispose(self, act, ledgers, ssh, http, sandbox, trace):
        ledgers.seed(resumed={EID: {"at": "x"}},
                     cancelled={LABEL: self._hist(time.time() - 90000, 2)})  # 都 >24h 前
        out = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert out["breaker"] == [] and out["cancelled"] == [LABEL]

    def test_second_cancel_still_allowed(self, act, ledgers, ssh, http, sandbox, trace):
        ledgers.seed(resumed={EID: {"at": "x"}})
        assert act(_dispose_inputs([_row()], duty_dir=ledgers.dir))["cancelled"] == [LABEL]
        kept = ledgers.read()["cancelled"]
        ledgers.seed(resumed={"eid2": {"at": "x"}}, cancelled=kept)
        out = act(_dispose_inputs([_row(exec_id="eid2")],
                                  instances=[{"id": SANDBOX_ID, "exec": "eid2"}],
                                  duty_dir=ledgers.dir))
        assert out["cancelled"] == [LABEL] and out["breaker"] == []
        assert len(ledgers.read()["cancelled"][LABEL]) == 2


class TestSalvageScript:
    """快照脚本本身要真跑（测试其余用例只打桩 ssh，容易掩盖脚本里的语法/路径错）。

    做法：把 ssh 换成本地 `python3 - <同一份脚本>`，在临时 git worktree 上跑真 git。
    """

    def test_patch_written_from_real_worktree(self, act, ledgers, http, sandbox, monkeypatch,
                                              tmp_path):
        import subprocess as sp

        wt = tmp_path / "recursive" / ".worktrees" / "issue-145"
        wt.mkdir(parents=True)
        (wt / "src.py").write_text("a = 1\n", encoding="utf-8")
        git = ["git", "-C", str(wt), "-c", "user.email=t@e", "-c", "user.name=t"]
        sp.run(git + ["init", "-q"], check=True)
        sp.run(git + ["add", "-A"], check=True)
        sp.run(git + ["commit", "-qm", "wip(issue-145): 半成品"], check=True)
        (wt / "src.py").write_text("a = 2\n", encoding="utf-8")   # 未提交改动

        run_dir = tmp_path / "pipeline" / "recursive-145"
        real_run = sp.run

        def local_ssh(cmd, **kw):
            src = kw.get("input") or ""
            if "issue_keeper" in src:
                return types.SimpleNamespace(stdout=json.dumps({"rc": 0}), stderr="",
                                             returncode=0)
            return real_run(["python3", "-"], input=src, capture_output=True, text=True,
                            timeout=60)   # 快照脚本本地真跑
        monkeypatch.setattr("subprocess.run", local_ssh)

        ledgers.seed(resumed={EID: {"at": "x"}})
        out = act(_dispose_inputs([_row(run_dir=str(run_dir), worktree_dir=str(wt))],
                                  duty_dir=ledgers.dir))
        assert out["cancelled"] == [LABEL]
        patch = run_dir / "salvage-snapshot.patch"
        assert patch.exists()
        body = patch.read_text(encoding="utf-8")
        assert "-a = 1" in body and "+a = 2" in body
        assert "wip(issue-145): 半成品" in body
        assert (ledgers.read()["cancelled"][LABEL][0]["salvage"] or {}).get("ok") is True

    def test_missing_worktree_reports_why_and_still_disposes(self, act, ledgers, http, sandbox,
                                                             monkeypatch, tmp_path):
        import subprocess as sp
        real_run = sp.run

        def local_ssh(cmd, **kw):
            src = kw.get("input") or ""
            if "issue_keeper" in src:
                return types.SimpleNamespace(stdout=json.dumps({"rc": 0}), stderr="",
                                             returncode=0)
            return real_run(["python3", "-"], input=src, capture_output=True, text=True,
                            timeout=60)
        monkeypatch.setattr("subprocess.run", local_ssh)

        ledgers.seed(resumed={EID: {"at": "x"}})
        out = act(_dispose_inputs([_row(run_dir=str(tmp_path / "nope"),
                                        worktree_dir="/gone/.worktrees/issue-145")],
                                  duty_dir=ledgers.dir))
        assert out["cancelled"] == [LABEL]        # 无产物可救也要解卡
        assert not (tmp_path / "nope" / "salvage-snapshot.patch").exists()
        warn = [f for f in out["findings"] if "salvage 快照未落盘" in f["summary"]]
        assert len(warn) == 1


class TestReopenFailureClosure:
    """复评阻断项：reopen 失败的 critical 不能被 finish 的「闭环降级」吞掉。

    `cancelled` 是按 issue label 记的，而**失败结论的 label 同样在里面**（cancel 已
    发生）——降级判据与递单排除若只看 label，就把「已 cancel 但单子仍卡终态」洗成
    warn：轮次不进 critical、不递单，值守必查清单看不到，正是 #22 要消灭的无人收尸
    （2026-10-10 实测：reopen-failure 轮 status=attention / ticket=None）。
    """

    def test_reopen_failure_stays_critical_and_raises(self, act, finish, ledgers, ssh, http,
                                                     sandbox, monkeypatch):
        ledgers.seed(resumed={EID: {"at": "x"}})
        ssh.reopen_rc = 2
        act_out = act(_dispose_inputs([_row()], duty_dir=ledgers.dir))
        assert act_out["cancelled"] == [LABEL]

        ticket = _FakeTicket()
        monkeypatch.setattr("subprocess.run", ticket)
        out = finish({"report": "在途 1（终态滞后 0 / 零进展 1 / 双120死档 1）",
                      "findings": [{"severity": "critical", "label": LABEL,
                                    "summary": "recursive#145 末节点结束已 150 分钟——双 120 命中"}],
                      "resumed": act_out["resumed"], "rows": [_row()], "outcome": {},
                      "cancelled": act_out["cancelled"], "act_findings": act_out["findings"],
                      "reopen_failed": act_out["reopen_failed"],
                      "requests_script": "duty_request.py", "duty_dir": ledgers.dir})

        assert out["status"] == "critical"
        assert len(ticket.cmds) == 1 and "--kind inflight-stall" in ticket.cmds[0]
        rnd = json.loads((pathlib.Path(ledgers.dir) / "state-inflight.json")
                         .read_text(encoding="utf-8"))["rounds"][-1]
        assert any(f["severity"] == "critical" and "reopen 未成功" in f["summary"]
                   for f in rnd["findings"])
        # 闭环那一条（triage）仍降级——成功路径的语义不许被这次改动误伤
        assert any("本轮已机械处置" in f["summary"] for f in rnd["findings"])
        # 留痕要写失败，不能照抄「随 cancel 自动 reopen」
        caps = {a["capability"]: a["summary"] for a in rnd["actions"]}
        assert "未成功" in caps["reopen_issue"] and LABEL in caps["reopen_issue"]


class _FakeTicket:
    def __init__(self):
        self.cmds = []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        return types.SimpleNamespace(
            stdout=json.dumps({"id": "req-20261009-000000-aaaaaa"}) + "\n",
            stderr="", returncode=0)


class TestRoundReport:
    """留痕：actions 记 capability/level；已闭环的不刷单，未处置的才递单。"""

    @staticmethod
    def _finish_input(duty_dir, findings, cancelled=(), act_findings=(), **kw):
        base = {"report": "在途 1（终态滞后 0 / 零进展 1 / 双120死档 1）",
                "findings": list(findings), "resumed": [], "requests_script": "duty_request.py",
                "rows": [], "outcome": {}, "cancelled": list(cancelled),
                "act_findings": list(act_findings), "duty_dir": duty_dir}
        base.update(kw)
        return base

    def test_actions_and_log_record_disposal(self, finish, tmp_path, monkeypatch):
        ticket = _FakeTicket()
        monkeypatch.setattr("subprocess.run", ticket)
        out = finish(self._finish_input(
            str(tmp_path),
            findings=[{"severity": "critical", "label": LABEL, "summary": "recursive#145 双 120"}],
            cancelled=[LABEL],
            act_findings=[{"severity": "warn", "source": "auto-dispose", "label": LABEL,
                           "summary": "recursive#145 双 120 死档 → 自动 cancel+reopen"}]))
        doc = json.loads((tmp_path / "state-inflight.json").read_text(encoding="utf-8"))
        caps = [a["capability"] for a in doc["rounds"][-1]["actions"]]
        assert caps == ["cancel_execution", "reopen_issue"]
        assert all(a["level"] == "authorized" for a in doc["rounds"][-1]["actions"])
        # `at` 是 state.schema.json 的 Action.required（复评观察 2，三条都补了）
        assert all(a["at"] for a in doc["rounds"][-1]["actions"])
        assert "机械处置=recursive#145" in (tmp_path / "rounds.log").read_text(encoding="utf-8")
        assert "机械处置 recursive#145" in doc["rounds"][-1]["narrative"]
        # 已闭环的单不再刷 inflight-stall 工单，也不再把轮次标成 critical
        assert ticket.cmds == []
        assert out["request"]["id"] is None
        assert out["status"] == "attention"
        assert [f["severity"] for f in doc["rounds"][-1]["findings"]] == ["warn", "warn"]
        assert "本轮已机械处置" in doc["rounds"][-1]["findings"][0]["summary"]

    def test_undealt_stall_still_raises_ticket(self, finish, tmp_path, monkeypatch):
        ticket = _FakeTicket()
        monkeypatch.setattr("subprocess.run", ticket)
        finish(self._finish_input(
            str(tmp_path),
            findings=[{"severity": "critical", "label": LABEL, "summary": "recursive#145 双 120"}]))
        assert len(ticket.cmds) == 1 and "--kind inflight-stall" in ticket.cmds[0]

    def test_breaker_raises_duty_ticket(self, finish, tmp_path, monkeypatch):
        """验收 3：熔断的那一单必须出现值守工单。"""
        ticket = _FakeTicket()
        monkeypatch.setattr("subprocess.run", ticket)
        finish(self._finish_input(
            str(tmp_path),
            findings=[{"severity": "critical", "label": LABEL, "summary": "recursive#145 双 120"}],
            act_findings=[{"severity": "critical", "source": "auto-dispose", "label": LABEL,
                           "summary": "recursive#145 双 120 死档但 24h 内已自动 cancel 2 次——已熔断"}]))
        assert len(ticket.cmds) == 1
        assert "--kind inflight-stall" in ticket.cmds[0]
        assert "熔断" in ticket.cmds[0]
