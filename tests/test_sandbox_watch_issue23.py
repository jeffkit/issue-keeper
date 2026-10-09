"""#23 回归——sandbox-watch 卡死判据：先证活，再判死。

实证（2026-10-09 19:00 轮）：impl 节点预算 7200s，节点边界只在节点结束落一条，
旧判据 `upd > stall_min(30)` 把任何跑过 30 分钟的健康长 impl 全判成「卡死」——
两例反证（沙箱内 agent 存活 57 分钟 / pytest 在跑）均为误报。

新判据（本文件钉死的行为）：
- 候选 = running + upd > stall_min + 实例存活 ≥0.75h（筛选，不定罪）；
- 预算门：有开节点（started 无 ended）且 upd < 预算+300s → 不报；
- 活性门：超预算/无开节点 → 进沙箱探活；agent 存活（etime≥120s）→ 不报；
  已退出 / 仅剩秒级新进程（崩溃循环）→ 报卡死且 evidence.agent_alive=False；
  探针故障 → 降级 warn「无法证伪」，不误杀也不静默放过；
- 孤儿判定（终态/unknown 执行的实例）行为不变。

探针打桩方式：code 节点进程内 `from e2b import Sandbox`，测试往 sys.modules
塞一个 fake e2b 模块控制 ps 输出（与 plaita 沙箱 subprocess 内的真实 import
路径一致）。
"""
import json
import pathlib
import sys
import types

import pytest

HERE = pathlib.Path(__file__).resolve().parent
FLOW_JSON = HERE.parent / "flows" / "sandbox-watch.flow.json"


class _Res:
    def __init__(self, out):
        self.stdout = out


class _Cmds:
    def __init__(self, out):
        self._out = out

    def run(self, cmd, timeout=45):
        return _Res(self._out)


class _Sbx:
    def __init__(self, out):
        self.commands = _Cmds(out)


class _FakeSandbox:
    """伪 AGS SDK：`ps` 输出由 out 控制；boom 非 None 时 connect 即抛。"""

    out = ""
    boom = None

    @classmethod
    def connect(cls, sid, **kw):
        if cls.boom is not None:
            raise cls.boom
        return _Sbx(cls.out)


@pytest.fixture()
def e2b_fake(monkeypatch):
    fake = types.ModuleType("e2b")
    fake.Sandbox = _FakeSandbox
    monkeypatch.setitem(sys.modules, "e2b", fake)
    _FakeSandbox.out = ""
    _FakeSandbox.boom = None
    return _FakeSandbox


def _triage():
    ir = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
    spec = next(n for n in ir["nodes"] if n.get("id") == "triage")
    ns: dict = {}
    exec(spec["code"], ns)  # code 节点：取 run(input) 直接驱动
    return ns["run"]


def _inst(**kw):
    base = {
        "id": "amuvsy4n2jipoktbkbssnovo23bfll2lshpsq2pf",
        "short": "amuvsy4n2jipok",
        "age_h": 2.2,
        "exec": "dbd5e8193aa94bb3",
        "state": "running",
        "exec_status": "running",
        "exec_update_age_sec": 48 * 60,
        "open_node": "impl",
        "last_node_end_age_sec": 49 * 60,
        "impl_budget_sec": 7200,
        "flow_id": "self-improve-v2",
        "goal": "#23 fix(sandbox-watch)",
        "node_summary": {},
    }
    base.update(kw)
    return base


@pytest.fixture()
def run():
    return _triage()


class TestHealthyLongImpl:
    """验收 1：impl 跑 60min+、无节点边界更新、agent 存活 → 不得报卡死。"""

    def test_open_node_within_budget_not_stalled(self, run):
        out = run({"instances": [_inst(exec_update_age_sec=60 * 60)],
                   "stall_min": 30})
        assert out["stalled"] == []
        assert all("卡死" not in f["summary"] for f in out["findings"])

    def test_alive_agent_over_budget_not_stalled(self, run, e2b_fake):
        # 19:00 轮实例到晚间形态：upd 133min > budget，但沙箱内 agent etime 3480s
        e2b_fake.out = "4732 3480 recursive --workspace /x run goal\n"
        out = run({"instances": [_inst(exec_update_age_sec=133 * 60)], "stall_min": 30})
        assert out["stalled"] == []
        assert out["findings"] == []

    def test_incident_form_suppressed_without_probe(self, run, e2b_fake):
        # 19:00 轮原始误报形态：upd=48min、impl 开节点、预算内 → 预算门直接放行，
        # 不应进沙箱探针（boom 以证明探针未被调用）
        e2b_fake.boom = RuntimeError("should not connect")
        out = run({"instances": [_inst()], "stall_min": 30})
        assert out["stalled"] == []
        assert out["findings"] == []

    def test_open_node_near_budget_edge_margin(self, run):
        # upd = budget + 299s 仍在容忍带内
        out = run({"instances": [_inst(exec_update_age_sec=7200 + 299)],
                   "stall_min": 30})
        assert out["stalled"] == []

    def test_missing_budget_falls_back_to_default_7200(self, run):
        it = _inst(impl_budget_sec=None)
        out = run({"instances": [it], "stall_min": 30})
        assert out["stalled"] == []

    def test_budget_override_from_schedule_params(self, run):
        it = _inst(impl_budget_sec=None, exec_update_age_sec=7300)
        out = run({"instances": [it], "stall_min": 30, "default_impl_budget": 14400})
        assert out["stalled"] == []


class TestRealStall:
    """验收 2：agent 进程已消失 + upd > budget → 必须报卡死 + 证据字段。"""

    def test_dead_agent_over_budget_reported(self, run, e2b_fake):
        e2b_fake.out = ""  # 沙箱里已无 recursive/pytest 进程
        out = run({"instances": [_inst(open_node="", exec_update_age_sec=133 * 60)],
                   "stall_min": 30})
        assert len(out["stalled"]) == 1
        f = out["findings"][0]
        assert f["severity"] == "warn"
        assert "agent 已退出" in f["summary"]
        ev = f["evidence"]
        assert ev["agent_alive"] is False
        assert ev["upd_min"] == 133
        assert ev["budget_sec"] == 7200

    def test_crash_loop_fresh_process_reported(self, run, e2b_fake):
        # 只剩秒级新进程：崩溃循环，不可当活证
        e2b_fake.out = "500 30 recursive --workspace /x run y\n"
        out = run({"instances": [_inst(open_node="")], "stall_min": 30})
        assert len(out["stalled"]) == 1
        assert "崩溃循环" in out["findings"][0]["summary"]

    def test_pytest_only_also_counts_alive(self, run, e2b_fake):
        # 19:09 反证第二例：agent 在跑体系提示词要求的 pytest 自验门
        e2b_fake.out = "26001 1900 python3 -m pytest tests -q\n"
        out = run({"instances": [_inst()], "stall_min": 30})
        assert out["stalled"] == []


class TestProbeFailureDegrades:
    """探针故障：绝不静默放过，也不误报卡死——降级 warn 提示人工核实。"""

    def test_probe_unavailable_downgrades_to_warn(self, run, e2b_fake):
        e2b_fake.boom = RuntimeError("connect refused")
        it = _inst(open_node="", exec_update_age_sec=8000)
        out = run({"instances": [it], "stall_min": 30})
        assert out["stalled"] == []
        warns = [f for f in out["findings"] if "无法证伪" in f["summary"]]
        assert len(warns) == 1
        assert warns[0]["severity"] == "warn"


class TestOrphanUnchanged:
    """边界：孤儿（终态执行实例）判定行为不变。"""

    def test_terminal_exec_instance_is_orphan(self, run):
        it = _inst(exec_status="completed", age_h=1.2,
                   exec_update_age_sec=None, open_node="")
        out = run({"instances": [it], "stall_min": 30})
        assert len(out["orphans"]) == 1
        assert any("孤儿实例" in f["summary"] for f in out["findings"])

    def test_unknown_exec_instance_is_orphan(self, run):
        it = _inst(exec_status="unknown", age_h=1.0)
        out = run({"instances": [it], "stall_min": 30})
        assert len(out["orphans"]) == 1

    def test_fresh_terminal_instance_not_orphan(self, run):
        it = _inst(exec_status="completed", age_h=0.2)
        out = run({"instances": [it], "stall_min": 30})
        assert out["orphans"] == []


class TestSelectionGate:
    """stall_min 退回筛选用途：不满足候选条件的一律不进判定（也不探针）。"""

    def test_young_instance_skipped(self, run, e2b_fake):
        it = _inst(age_h=0.3, open_node="", exec_update_age_sec=9000)
        out = run({"instances": [it], "stall_min": 30})
        assert out["findings"] == []

    def test_recent_update_skipped_no_probe(self, run, e2b_fake):
        # 探针 boom 也不会被触发：未进候选即 continue
        e2b_fake.boom = RuntimeError("should not connect")
        it = _inst(exec_update_age_sec=10 * 60)
        out = run({"instances": [it], "stall_min": 30})
        assert out["findings"] == []

    def test_terminal_status_never_stalled(self, run):
        it = _inst(exec_status="failed", exec_update_age_sec=9000)
        out = run({"instances": [it], "stall_min": 30})
        assert out["stalled"] == []


class TestQuotaAndErrorUnchanged:
    def test_quota_pressure(self, run):
        out = run({"instances": [_inst(id="s%d" % i, exec="") for i in range(6)],
                   "quota_warn": 6})
        assert any("配额压力" in f["summary"] for f in out["findings"])

    def test_lister_error_critical(self, run):
        out = run({"instances": [], "error": "boom", "stall_min": 30})
        assert any(f["severity"] == "critical" for f in out["findings"])
