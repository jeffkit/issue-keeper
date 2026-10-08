"""RECURSIVE_RUN_DEADLINE 注入的回归测试（2026-10-02 评审遗留 #3）。

recursive v3 宿主读该 env（epoch 秒）做 run 级预算墙，到点优雅退出
（verdict/台账带 node_retry_exhausted，reaper 不再自动重派）。此前 keeper
从不注入——宿主恒走进程内默认 8h，与 keeper 侧 pipeline_timeout_secs 的
killpg 护栏脱节：keeper 先 SIGKILL，宿主没机会 checkpoint/回评。

注入语义（keeper._engine_env_with_run_deadline）：
- 注入值 = 派发时刻 + pipeline_timeout_secs - run_deadline_margin_secs，
  且与 reaper 基线（it.in_flight_since）同一时钟读数；
- engine_env 显式配置优先，不被覆盖；
- 仅 engine=v2 注入；margin ≥ 预算（无优雅窗口）跳过注入退回 killpg。
"""

import json
import time

from issue_keeper.config import Config, PipelineRepoConfig, RepoBinding
from issue_keeper.keeper import _dispatch_pipeline, _engine_env_with_run_deadline
from issue_keeper.state import ItemState


def _res(number: int = 7):
    from issue_keeper.sources import Resource
    return Resource(
        kind="issue", number=number, title="t", body="正文", state="open",
        labels=[], author="bob", created_at="", updated_at="",
        status="inbox", actor_type="human",
    )


def _cfg(bridge, **over) -> Config:
    base = dict(
        pipeline_bridge=bridge,
        pipeline_timeout_secs=3600,
        run_deadline_margin_secs=300,
        pipeline_claim_comment=False,
    )
    base.update(over)
    return Config(**base)


def _copying_bridge(tmp_path):
    """假 bridge：把 dispatch.json 复制一份供断言（照 test_pipeline_dispatch_guard 惯例）。

    文件名必须是 v2_bridge.py——engine=v2 派发时 keeper 会把 bridge 换成
    传入路径同目录的 v2_bridge.py（keeper._dispatch_pipeline）。"""
    bridge = tmp_path / "v2_bridge.py"
    bridge.write_text(
        "import json, shutil, sys\n"
        "shutil.copy(sys.argv[1], sys.argv[1] + '.seen.json')\n",
        encoding="utf-8",
    )
    return bridge


def _wait_seen(art, timeout=5.0):
    seen = art / "dispatch.json.seen.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if seen.exists():
            # exists 先于内容可见：shutil.copy 先建目标文件再写内容，
            # 满载下轮询会读到空文件（JSONDecodeError char 0）——重读即可
            try:
                return json.loads(seen.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        time.sleep(0.05)
    assert seen.exists(), "假 bridge 未落 dispatch.json 副本"
    return json.loads(seen.read_text(encoding="utf-8"))


def _art(tmp_path, repo="b", number=7):
    art = tmp_path / ".issue-keeper" / "pipeline" / f"{repo}-{number}"
    art.mkdir(parents=True, exist_ok=True)
    return art


# ── helper 级：注入值公式 ────────────────────────────────────────────

def test_helper_injects_start_plus_budget_minus_margin():
    cfg = _cfg("/no-such-bridge", pipeline_timeout_secs=5400,
               run_deadline_margin_secs=300)
    pc = PipelineRepoConfig(engine="v2", test_command="true")
    env = _engine_env_with_run_deadline(cfg, pc, 1000.0, "t")
    assert env["RECURSIVE_RUN_DEADLINE"] == str(int(1000.0 + 5400 - 300))


def test_helper_explicit_deadline_wins():
    cfg = _cfg("/no-such-bridge")
    pc = PipelineRepoConfig(engine="v2", test_command="true",
                            engine_env={"RECURSIVE_RUN_DEADLINE": "9999999999",
                                        "RECURSIVE_HOST_V3": "1"})
    env = _engine_env_with_run_deadline(cfg, pc, 1000.0, "t")
    # 显式值不被覆盖，其余键原样保留
    assert env == {"RECURSIVE_RUN_DEADLINE": "9999999999",
                   "RECURSIVE_HOST_V3": "1"}


def test_helper_pipeline_engine_not_injected():
    cfg = _cfg("/no-such-bridge")
    pc = PipelineRepoConfig(engine="pipeline", test_command="true")
    assert _engine_env_with_run_deadline(cfg, pc, 1000.0, "t") == {}


def test_helper_skips_when_no_grace_window():
    """margin ≥ 预算：注入一个秒触发的假 deadline 比不注入更糟，跳过退回 killpg。"""
    cfg = _cfg("/no-such-bridge", pipeline_timeout_secs=300,
               run_deadline_margin_secs=300)
    pc = PipelineRepoConfig(engine="v2", test_command="true")
    assert _engine_env_with_run_deadline(cfg, pc, 1000.0, "t") == {}


# ── dispatch 级：payload 落盘与 reaper 基线对齐 ──────────────────────

def test_dispatch_injects_deadline_aligned_with_reaper_baseline(tmp_path, monkeypatch):
    """注入值 = in_flight_since + pipeline_timeout_secs - margin（同一时钟读数）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = _copying_bridge(tmp_path)
    art = _art(tmp_path)
    it = ItemState()
    cfg = _cfg(bridge, pipeline_timeout_secs=3600, run_deadline_margin_secs=300)
    pc = PipelineRepoConfig(engine="v2", test_command="true",
                            engine_env={"RECURSIVE_HOST_V3": "1"})

    t0 = time.time()
    out = _dispatch_pipeline(cfg, RepoBinding(repo="a/b", profile="p"),
                             _res(7), it, "a/b issue#7", pc=pc)
    t1 = time.time()

    assert out["status"] == "dispatched"
    payload = _wait_seen(art)
    env = payload["engine_env"]
    # 其他 engine_env 键原样透传
    assert env["RECURSIVE_HOST_V3"] == "1"
    dl = int(env["RECURSIVE_RUN_DEADLINE"])
    # 与 reaper 基线严格同源：in_flight_since + 预算 - margin（精确相等）
    assert dl == int(it.in_flight_since + 3600 - 300)
    # 且落在派发时刻的合理窗口内（防 in_flight_since 语义漂移；int 取整容差 1s）
    assert int(t0 + 3300) <= dl <= int(t1 + 3300)


def test_dispatch_explicit_deadline_not_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = _copying_bridge(tmp_path)
    art = _art(tmp_path)
    cfg = _cfg(bridge)
    pc = PipelineRepoConfig(engine="v2", test_command="true",
                            engine_env={"RECURSIVE_RUN_DEADLINE": "4102444800"})

    out = _dispatch_pipeline(cfg, RepoBinding(repo="a/b", profile="p"),
                             _res(7), ItemState(), "a/b issue#7", pc=pc)
    assert out["status"] == "dispatched"
    payload = _wait_seen(art)
    assert payload["engine_env"]["RECURSIVE_RUN_DEADLINE"] == "4102444800"


def test_dispatch_default_margin_without_yaml_key(tmp_path, monkeypatch):
    """yaml 未配 run_deadline_margin_secs：默认 300 生效（窗口 = 预算 - 300）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = _copying_bridge(tmp_path)
    art = _art(tmp_path)
    cfg = Config(pipeline_bridge=bridge, pipeline_timeout_secs=3600,
                 pipeline_claim_comment=False)  # 不给 run_deadline_margin_secs
    pc = PipelineRepoConfig(engine="v2", test_command="true")

    t0 = time.time()
    out = _dispatch_pipeline(cfg, RepoBinding(repo="a/b", profile="p"),
                             _res(7), ItemState(), "a/b issue#7", pc=pc)
    t1 = time.time()
    assert out["status"] == "dispatched"
    payload = _wait_seen(art)
    dl = int(payload["engine_env"]["RECURSIVE_RUN_DEADLINE"])
    assert int(t0 + 3300) <= dl <= int(t1 + 3300)  # 默认 margin=300 → 窗口 3300
    assert dl > t0  # 未来时刻（epoch 秒）


def test_dispatch_pipeline_engine_payload_has_no_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    bridge = _copying_bridge(tmp_path)
    art = _art(tmp_path)
    cfg = _cfg(bridge)
    pc = PipelineRepoConfig(engine="pipeline", test_command="true")

    out = _dispatch_pipeline(cfg, RepoBinding(repo="a/b", profile="p"),
                             _res(7), ItemState(), "a/b issue#7", pc=pc)
    assert out["status"] == "dispatched"
    payload = _wait_seen(art)
    assert "RECURSIVE_RUN_DEADLINE" not in payload["engine_env"]
