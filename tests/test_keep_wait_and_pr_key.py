"""issue #10 回归测试。

覆盖两个缺陷：

1. `python -m issue_keeper keep --once --wait` 分支引用未定义名
   `_count_in_flight_module`（`issue_keeper/__main__.py`，第 30 行导入的是
   `_count_in_flight`）→ 一执行即 NameError；且循环体内 reaper 只改内存、
   从不落盘，下一轮 load 又读回旧盘 → 在途计数永不归零（空转）。
2. `keeper._reap_pipelines` 里 `int(key)` 遇 PR key（`"pr:42"`，见
   `sources/__init__.py` 的 `resource_key`）抛 ValueError，异常冒到 run_once，
   该轮之后所有条目不收尾且不落盘。
"""

import json
import time

from issue_keeper import __main__ as cli
from issue_keeper import keeper
from issue_keeper.config import Config, RepoBinding
from issue_keeper.state import State, load_state, save_state


def _cfg(tmp_path, **over) -> Config:
    base = dict(
        state_file=tmp_path / "state.json",
        repos=[RepoBinding(repo="a/b", profile="p")],
        pipeline_bridge=tmp_path / "bridge.py",
        pipeline_timeout_secs=30,
        pipeline_push_mode="branch",
        pipeline_review_mode="auto",
        pipeline_test_commands={},
    )
    base.update(over)
    return Config(**base)


def test_keep_once_wait_exits_and_persists(tmp_path, monkeypatch, capsys):
    """--wait：reaper 收敛内存状态后本轮计数归零 → 打印完成、退出码 0、盘上清在途。"""
    cfg = _cfg(tmp_path)
    state = State()
    state.repo("a-b").item("7").in_flight_since = time.time()
    save_state(cfg.state_path, state)

    monkeypatch.setattr(cli, "load_config", lambda _p: cfg)
    monkeypatch.setattr(cli, "run_once", lambda _c: 3)

    reap_calls = []

    def _reap(config, state, bindings=None):
        reap_calls.append((config, bindings))
        assert bindings is not None, "reaper 需要 bindings（config.repos）"
        state.repo("a-b").item("7").in_flight_since = None  # reaper 收敛内存状态
        return 1

    monkeypatch.setattr(keeper, "_reap_pipelines", _reap)

    sleeps = []

    def _sleep(secs):
        sleeps.append(secs)
        if len(sleeps) > 3:
            raise AssertionError("--wait 循环未退出：计数读回旧盘（reaper 结果未落盘）")

    monkeypatch.setattr(time, "sleep", _sleep)

    rc = cli.main(["keep", "--config", str(tmp_path / "config.yaml"), "--once", "--wait"])

    assert rc == 0
    assert "完成，本轮处理 3 条。" in capsys.readouterr().out
    assert len(reap_calls) == 1
    assert load_state(cfg.state_path).repo("a-b").item("7").in_flight_since is None


def test_reaper_handles_pr_key(tmp_path, monkeypatch):
    """key="pr:42" 走 reaper：不抛 ValueError，代记台账按 issue=42 落账并进自动重派。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(tmp_path)
    state = State()
    it = state.repo("a-b").item("pr:42")
    it.in_flight_since = time.time()
    # artifact 目录派生：slug = repo.split("/")[-1]，key 原样（含 "pr:" 前缀）
    art = tmp_path / ".issue-keeper" / "pipeline" / "b-pr:42"
    art.mkdir(parents=True, exist_ok=True)
    (art / "00-issue.md").write_text("正文", encoding="utf-8")
    # 无 run.lock、无台账 → reaper 走「bridge 已退出且未写台账」代记路径
    monkeypatch.setattr(keeper, "_channel_reply_posted", lambda *a, **kw: False)
    monkeypatch.setattr(keeper, "_gh_post_comment", lambda *a, **kw: None)

    assert keeper._reap_pipelines(cfg, state, [RepoBinding(repo="a/b", profile="p")]) == 1

    assert it.in_flight_since is None
    assert it.processed is False  # 首败 → 自动重派，不消费首响应
    rows = [
        json.loads(line)
        for line in (tmp_path / ".issue-keeper" / "pipeline" / "runs.jsonl")
        .read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows and rows[0]["repo"] == "a/b"
    assert rows[0]["issue"] == 42 and rows[0]["status"] == "engine_error"
