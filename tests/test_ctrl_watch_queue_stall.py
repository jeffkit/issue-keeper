"""ctrl-watch「在途空转」判据的回归测试（2026-10-11 无限交接风暴后补）。

## 为什么要有这个测试

2026-10-11 11:21–13:40，两台 plaita worker 因「宿主归属自锁」**全线空转**：
近 300 行日志 292 行是「不亲和」、6 条在途零产出、队列恒定 6。而**所有心跳
A/B/shadow 全部健康** ⇒ ctrl-watch 连报 3 轮（12:30/13:00/13:30）「全线正常」。

**根因是判据缺口，不是阈值问题**：ctrl-watch 只验「进程活着」（心跳新鲜度），
不验「工作在进行」（产能）。看门狗自己没死，但整个体系产能为零 —— 这是
meta-watchdog 的盲区。

本测试钉死新判据的三条边界：
1. **停摆要报**（积压且两轮完全相同 ⇒ critical）；
2. **正常不许误报**（队列在流动 / 队列为空 / 采样间隔太短 / 读数不可用）；
3. 采样不可用时**不报**（那是另一类故障，由心跳/shadow 判据覆盖）。
"""
from __future__ import annotations

import json
import pathlib

import pytest

FLOW_JSON = pathlib.Path(__file__).resolve().parents[1] / "flows" / "ctrl-watch.flow.json"


def _node_code(node_id: str) -> str:
    """取出 flow JSON 里指定节点的可执行源码（**产物**，即线上真正跑的）。"""
    doc = json.loads(FLOW_JSON.read_text(encoding="utf-8"))
    for n in doc.get("nodes") or []:
        if n.get("id") == node_id:
            code = n.get("code") or (n.get("config") or {}).get("code")
            if code:
                return code
    raise AssertionError("节点 %s 未在 %s 里找到" % (node_id, FLOW_JSON))


@pytest.fixture(scope="module")
def triage():
    ns: dict = {}
    exec(compile(_node_code("triage"), "<triage>", "exec"), ns)
    return ns["run"]


def _base(**kw):
    """一轮健康输入；用例只覆盖关心的字段。"""
    d = {
        "now": 1_000_000.0,
        "a": {"round": 1, "at": "2026-10-11T13:30:00+08:00"},
        "a_ts": 999_400.0,
        "b": {"at": "2026-10-11 13:30", "line": "B-flow 轮次=1"},
        "b_ts": 999_400.0,
        "shadow_age": 60,
        "a_stale_min": 25,
        "b_stale_min": 180,
        "shadow_stale_min": 20,
        "qlen_now": None,
        "qlen_prev": None,
        "qlen_prev_at": None,
    }
    d.update(kw)
    return d


def _stall_summaries(res):
    return [f["summary"] for f in res["findings"] if "空转" in f["summary"]]


def test_healthy_all_green(triage):
    """一切正常（心跳新鲜 + 队列在流动）⇒ 不得有任何 finding。"""
    res = triage(_base(qlen_now=3, qlen_prev=7, qlen_prev_at=1_000_000.0 - 1500))
    assert res["findings"] == [], res["findings"]
    assert res["report"] == "全线正常"
    assert res["need_escalate"] is False


def test_stalled_queue_is_critical(triage):
    """★ 本日事故形态：队列积压且两轮完全相同 ⇒ critical + escalate。

    这正是 11:21–13:40 真实发生的情况（队列恒定 6–7、心跳全绿）。
    """
    res = triage(_base(qlen_now=7, qlen_prev=7, qlen_prev_at=1_000_000.0 - 1500))
    hits = _stall_summaries(res)
    assert len(hits) == 1, res["findings"]
    assert res["need_escalate"] is True
    crit = [f for f in res["findings"] if "空转" in f["summary"]]
    assert crit[0]["severity"] == "critical"
    assert crit[0]["escalate"] is True


def test_queue_moving_is_not_flagged(triage):
    """队列在流动（7→3）⇒ 不报（正常消费，不得误报）。"""
    res = triage(_base(qlen_now=3, qlen_prev=7, qlen_prev_at=1_000_000.0 - 1500))
    assert _stall_summaries(res) == []


def test_empty_queue_is_not_flagged(triage):
    """队列为空（空闲）⇒ 不报。空队列不是停摆。"""
    res = triage(_base(qlen_now=0, qlen_prev=0, qlen_prev_at=1_000_000.0 - 1500))
    assert _stall_summaries(res) == []


def test_short_interval_is_not_flagged(triage):
    """两次采样相隔太短（<20min）⇒ 不报（避免把「刚记基线」当停摆）。"""
    res = triage(_base(qlen_now=7, qlen_prev=7, qlen_prev_at=1_000_000.0 - 300))
    assert _stall_summaries(res) == []


def test_unreadable_queue_is_not_flagged(triage):
    """读数不可用（ssh/redis 失败 ⇒ None）⇒ **不报**。

    刻意如此：读不到是另一类故障，已由心跳/shadow 判据覆盖；
    在此处猜报会制造噪音（且今天的教训正是「误报/漏报都源于判据不清」）。
    """
    assert _stall_summaries(triage(_base(qlen_now=None))) == []
    assert _stall_summaries(triage(_base(qlen_now=None, qlen_prev=7))) == []
    # 首次运行（无基线）同样不报
    assert _stall_summaries(triage(_base(qlen_now=7))) == []


def test_boolean_is_not_treated_as_int(triage):
    """防御：True/False 是 int 子类，不得被当成队列长度（否则 True==1 可误报）。"""
    assert _stall_summaries(triage(_base(qlen_now=True, qlen_prev=True,
                                         qlen_prev_at=1_000_000.0 - 1500))) == []


def test_facts_node_reports_queue_length_field(triage):
    """facts 节点必须真的产出 qlen_* 字段（否则判据永远拿不到输入）。"""
    ns: dict = {}
    exec(compile(_node_code("facts"), "<facts>", "exec"), ns)
    out = ns["run"]({"duty_dir": "/tmp"})
    for k in ("qlen_now", "qlen_prev", "qlen_prev_at"):
        assert k in out, "facts 缺少字段 %s" % k
    # 取不到时必须是 None（而不是 0，否则会伪装成「空闲」）
    assert out["qlen_now"] is None or isinstance(out["qlen_now"], int)
