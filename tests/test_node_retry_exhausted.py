"""reaper 消费 node_retry_exhausted——宿主终局不自动重派（DESIGN-local-distributed-host §5 D6）。

背景：v3 宿主在节点重试耗尽 / run 预算墙后判 engine_error 并带
`node_retry_exhausted: True`（recursive .dev/flows/self_improve_bridge_v2.py
run_host_v3）。契约（recursive/.dev/docs/DESIGN-local-distributed-host.md §5）：
标记走台账 extra → keeper reaper 见标记**跳过 engine_error 自动重派**直接升级。
此前 v2_bridge 的 else 分支把标记丢掉、reaper 只见 status=engine_error，耗尽 run
仍被自动再派一轮（implement 1-2h）才升级，纯浪费。

三块契约：
1. v2_bridge：engine_error 台账行透传 node_retry_exhausted；
2. reaper：带标记 → 首败即升级（兜底回评 + processed 终态），不带标记 → 原语义
   分毫不动（首败重试、二连升级）；
3. 连击计数：耗尽行**打断** trailing 连击——它是宿主烧满重试后的完整战役终态，
   不是瞬态崩溃；不打断的话一次耗尽升级会吃掉其后新派发首败的重试额度
   （耗尽路径本身永远不查计数，打断零成本）。
"""
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

import pytest

from issue_keeper.keeper import _consecutive_engine_errors, _reap_pipelines
from tests.test_pipeline_dispatch_guard import (
    _bindings,
    _in_flight_state,
    _pipeline_cfg,
)


# ── 台账助手（显式控行，不用 _write_issue_ledger 的自动播种）────────────────

def _ledger_row(home: pathlib.Path, repo: str, number: int, **rec) -> None:
    ledger = home / ".issue-keeper" / "pipeline" / "runs.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    row = {"repo": repo, "issue": number,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S+0800"), **rec}
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


@pytest.fixture
def reap_env(tmp_path, monkeypatch):
    """与 test_issue4 同款隔离：HOME 指向 tmp、评论/渠道读回全部桩掉。

    追加返回 home，供 _ledger_row 与 _pipeline_cfg 定位（reaper 的 artifact/
    台账路径都经 expanduser 读 HOME）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    state, it, art = _in_flight_state(tmp_path, repo="a/b")
    posted = []
    monkeypatch.setattr("issue_keeper.keeper._gh_post_comment",
                        lambda kind, repo, number, body: posted.append(body))
    monkeypatch.setattr("issue_keeper.keeper._channel_reply_posted",
                        lambda *a, **kw: False)
    return state, it, art, posted, tmp_path


# ── 1. reaper：带标记 vs 不带标记的重派行为差异 ─────────────────────────────

def test_耗尽标记_首败即升级不自动重派(reap_env):
    """宿主已判节点重试耗尽 → 不再白烧一轮，直接升级（兜底回评 + 终态收尾）。

    判别力：同一台账若**不带**标记，首败 trailing=1 → 自动重试（不发评论）。
    本用例单条记录即升级，证明是标记在起作用而非连击计数。"""
    state, it, art, posted, home = reap_env
    _ledger_row(home, "a/b", 7, status="engine_error",
                error="node retries exhausted at impl: AgentRunError: x",
                node_retry_exhausted=True, comment_posted=False)

    _reap_pipelines(_pipeline_cfg(home / "b"), state, _bindings(repo="a/b"))

    assert len(posted) == 1, "耗尽必须首败即升级，不等二连"
    assert "node_retry_exhausted" in posted[0], "回评留痕：让人工看出为何不重派"
    assert "不自动重派" in posted[0]
    assert it.processed, "升级即终态收尾（processed），不再留在途"
    assert it.in_flight_since is None
    assert not (art / "run.lock").exists()


def test_无标记_首败仍自动重试_原语义不动(reap_env):
    """普通 engine_error（无标记）：首败仍清在途自动重派、不升级不发评论。"""
    state, it, art, posted, home = reap_env
    _ledger_row(home, "a/b", 7, status="engine_error",
                error="v2 run exit=1 without verdict", comment_posted=False)

    _reap_pipelines(_pipeline_cfg(home / "b"), state, _bindings(repo="a/b"))

    assert posted == [], "首败不允许升级"
    assert not it.processed
    assert it.in_flight_since is None, "清在途 = 下一轮自动重派"
    assert not (art / "run.lock").exists()


def test_耗尽升级后_新派发首败仍享重试额度(reap_env):
    """耗尽行打断连击的行为面验证：耗尽升级之后人工重开、新 run 普通崩溃——
    trailing 只数到 1 → 仍自动重试。若把耗尽行计入连击，这里会 trailing=2
    直接升级，新战役的首败重试额度被上一轮战役吃掉。"""
    state, it, art, posted, home = reap_env
    _ledger_row(home, "a/b", 7, status="engine_error",
                error="node retries exhausted at impl", node_retry_exhausted=True,
                comment_posted=False)
    _ledger_row(home, "a/b", 7, status="engine_error",
                error="bridge 进程已退出且未写台账", comment_posted=False)

    _reap_pipelines(_pipeline_cfg(home / "b"), state, _bindings(repo="a/b"))

    assert posted == [], "新战役首败应重试而非升级"
    assert not it.processed
    assert it.in_flight_since is None


def test_无标记_二连仍升级_文案不含耗尽标记(reap_env):
    """保护不带标记的原升级路径：二连升级、旧文案（管线引擎异常终止）不变。"""
    state, it, art, posted, home = reap_env
    _ledger_row(home, "a/b", 7, status="engine_error",
                error="boom-1", comment_posted=False)
    _ledger_row(home, "a/b", 7, status="engine_error",
                error="boom-2", comment_posted=False)

    _reap_pipelines(_pipeline_cfg(home / "b"), state, _bindings(repo="a/b"))

    assert len(posted) == 1
    assert "管线引擎异常终止" in posted[0]
    assert "node_retry_exhausted" not in posted[0]


# ── 2. 连击计数契约：耗尽行打断 trailing ───────────────────────────────────

@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".issue-keeper" / "pipeline").mkdir(parents=True)
    return tmp_path / ".issue-keeper" / "pipeline" / "runs.jsonl"


def test_耗尽行打断连击(ledger):
    _ledger_row(ledger.parents[2], "a/b", 9,
                status="engine_error", comment_posted=False)
    _ledger_row(ledger.parents[2], "a/b", 9, status="engine_error",
                node_retry_exhausted=True, comment_posted=False)
    _ledger_row(ledger.parents[2], "a/b", 9, status="engine_error",
                comment_posted=False)
    assert _consecutive_engine_errors("a/b", 9) == 1


def test_耗尽行自身不进连击(ledger):
    _ledger_row(ledger.parents[2], "a/b", 10, status="engine_error",
                node_retry_exhausted=True, comment_posted=False)
    _ledger_row(ledger.parents[2], "a/b", 10, status="engine_error",
                comment_posted=False)
    assert _consecutive_engine_errors("a/b", 10) == 1


# ── 3. v2_bridge：台账透传标记 ─────────────────────────────────────────────

HERE = pathlib.Path(__file__).resolve().parent
V2_BRIDGE = HERE.parent / "flows" / "v2_bridge.py"


def _load_v2_bridge():
    """加载 v2_bridge；加载期间临时放宽 sys.path（其顶层 import 仓外 plaita），
    加载完即恢复——防仓外树里的同名包污染本仓测试导入。"""
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("v2_bridge_under_test", V2_BRIDGE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = saved


try:
    v2b = _load_v2_bridge()
except Exception:  # 仓外依赖 plaita 不在时跳过（映射逻辑本身无外部依赖）
    pytest.skip("v2_bridge 依赖仓外 plaita，跳过", allow_module_level=True)


def _run_v2_main(tmp_path, monkeypatch, verdict) -> dict:
    """桩掉宿主子进程与 state.json 读取，跑 v2_bridge.main，返回落台账的记录。"""
    main_clone = tmp_path / "clone"
    (main_clone / ".dev" / "flows").mkdir(parents=True)
    (main_clone / ".dev" / "flows" / "self_improve_bridge_v2.py").write_text("# stub")
    art = tmp_path / "art"
    art.mkdir()
    payload_file = tmp_path / "dispatch.json"
    payload_file.write_text(json.dumps({
        "repo_full": "a/b", "issue_number": 7, "main_clone": str(main_clone),
        "artifact_dir": str(art),
    }), encoding="utf-8")

    captured: list[dict] = []
    monkeypatch.setattr(v2b.pb, "append_ledger", lambda rec: captured.append(rec))
    monkeypatch.setattr(
        v2b.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr=""))
    monkeypatch.setattr(v2b, "_read_verdict", lambda mc, rid: verdict)
    monkeypatch.setattr(sys, "argv", ["v2_bridge.py", str(payload_file)])
    v2b.main()
    assert captured, "台账必须落一行"
    return captured[0]


def test_v2_engine_error透传耗尽标记(tmp_path, monkeypatch):
    rec = _run_v2_main(tmp_path, monkeypatch,
                       {"verdict": "engine_error",
                        "why": "node retries exhausted at impl: x",
                        "node_retry_exhausted": True})
    assert rec["status"] == "engine_error"
    assert rec["node_retry_exhausted"] is True
    assert "node retries exhausted" in rec["error"]


def test_v2_普通engine_error不带标记(tmp_path, monkeypatch):
    rec = _run_v2_main(tmp_path, monkeypatch,
                       {"verdict": "engine_error", "why": "exit=1 without verdict"})
    assert rec["status"] == "engine_error"
    assert "node_retry_exhausted" not in rec


def test_v2_其他verdict不受影响(tmp_path, monkeypatch):
    rec = _run_v2_main(tmp_path, monkeypatch,
                       {"verdict": "retry-later", "stage": "host", "why": "disk"})
    assert rec["status"] == "retry-later"
    assert "node_retry_exhausted" not in rec
