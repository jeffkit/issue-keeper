"""评论层异步化（2026-10-01 后台化改造）的单元测试：派发登记、收尸发布、
超时击杀、重试封顶、全局并发上限、重启接管判活。

全部用假 src / monkeypatch，不调真 agent。
"""
from __future__ import annotations

import subprocess
import time
import types

from issue_keeper.config import Config, RepoBinding, ScreenerConfig
from issue_keeper.keeper import (
    _collect_comment_tasks,
    _count_comment_tasks,
    _process_resource,
    _spawn_comment_agent,
)
from issue_keeper.sources import Comment, Resource


def _cfg(**kw) -> Config:
    return Config(pipeline_mode=False, opt_out_labels=["keeper-ignore"], **kw)


def _res(number=7, status="inbox") -> Resource:
    return Resource(kind="issue", number=number, title="t", body="", state="open",
                    labels=[], author="alice", created_at="", updated_at="",
                    status=status, actor_type="human", source_ref="")


def _comment(cid="c1", author="okguitar") -> Comment:
    return Comment(id=cid, url="", author=author, body="请帮忙看下", created_at="")


def _binding() -> RepoBinding:
    return RepoBinding(repo="a/b", profile="p", agent_label="alpha-agent")


def _src(comments=None):
    return types.SimpleNamespace(
        move_status=lambda *a, **k: (True, "x"),
        list_comments=lambda *a, **k: comments or [],
        web_url=lambda *a, **k: "http://x/1",
    )


def _rs():
    def item(key):
        return types.SimpleNamespace(
            in_flight_since=None, processed=False, blocked=False,
            processed_comment_ids=set(), session_id=None, comment_tasks={})
    return types.SimpleNamespace(item=item)


def _state_with_tasks(count, pid=999999):
    """构造一个带 count 条在途评论任务的假 state（判活用不可能存在的 pid）。"""
    repos = {}
    for i in range(count):
        repos[f"r{i}"] = types.SimpleNamespace(items={
            f"issue:{i}": types.SimpleNamespace(comment_tasks={
                str(i): {"pid": pid, "started_at": time.time(), "attempts": 1}})})
    return types.SimpleNamespace(repos=repos)


def _screener():
    return ScreenerConfig(enabled=False, provider="openai", api_key=None,
                          base_url=None, model=None, on_unsafe="skip", max_chars=8000)


def _entry():
    return types.SimpleNamespace(name="fake", is_hub=False, cwd=None, env={},
                                 timeout_secs=0)


class TestSpawnAndCollect:
    def test_spawn_writes_files_and_returns_pid(self, tmp_path, monkeypatch):
        import issue_keeper.keeper as K
        monkeypatch.setattr(K, "_COMMENT_PROC_DIR", tmp_path)
        monkeypatch.setattr(K, "_build_command", lambda *a, **k: ["cat"])
        it = types.SimpleNamespace(session_id=None)
        pid = _spawn_comment_agent(_entry(), "你好", it, "a-b-7", "c1", "ik", 60)
        assert pid and pid > 0
        assert (tmp_path / "a-b-7.msg").read_text(encoding="utf-8") == "你好"
        proc = K._COMMENT_PROCS.pop(("a-b-7", "c1"))
        proc.wait(timeout=5)
        assert (tmp_path / "a-b-7.out").read_text(encoding="utf-8") == "你好"

    def test_collect_publishes_finished_reply(self, tmp_path, monkeypatch):
        import issue_keeper.keeper as K
        monkeypatch.setattr(K, "_COMMENT_PROC_DIR", tmp_path)
        (tmp_path / "a-b-7.out").write_text("这是回复", encoding="utf-8")
        (tmp_path / "a-b-7.err").write_text("agentproc:session:s-123", encoding="utf-8")
        published = []
        monkeypatch.setattr(K, "_publish_reply",
                            lambda *a, **k: published.append(a[3]))
        it = types.SimpleNamespace(
            processed_comment_ids=set(), session_id=None,
            comment_tasks={"c1": {"pid": 999999, "started_at": time.time(),
                                  "attempts": 1}})
        moved = []
        src = types.SimpleNamespace(
            move_status=lambda *a, **k: moved.append(a[2]) or (True, "x"))
        handled = _collect_comment_tasks(src, _binding(), _cfg(), _entry(), _res(),
                                         it, "a/b issue#7", 60, "[ik]")
        assert handled == 1 and published == ["这是回复"]
        assert "c1" in it.processed_comment_ids and it.comment_tasks == {}
        assert it.session_id == "s-123"
        assert "review" in moved

    def test_collect_retries_once_then_gives_up(self, tmp_path, monkeypatch):
        import issue_keeper.keeper as K
        monkeypatch.setattr(K, "_COMMENT_PROC_DIR", tmp_path)
        (tmp_path / "a-b-7.out").write_text("", encoding="utf-8")
        (tmp_path / "a-b-7.err").write_text("", encoding="utf-8")
        spawns = []
        monkeypatch.setattr(K, "_spawn_comment_agent",
                            lambda *a, **k: spawns.append(1) or 555555)  # 假 pid（不存在）
        it = types.SimpleNamespace(
            processed_comment_ids=set(), session_id=None,
            comment_tasks={"c1": {"pid": 999999, "started_at": time.time(),
                                  "attempts": 1}})
        # 第一次收尸：无输出 → 自动重试（attempts→2，换新 pid）
        _collect_comment_tasks(_src(), _binding(), _cfg(), _entry(), _res(),
                               it, "a/b issue#7", 60, "[ik]")
        assert it.comment_tasks["c1"]["attempts"] == 2
        assert it.comment_tasks["c1"]["pid"] == 555555  # 已重新 spawn（假 pid）
        # 第二次收尸：仍无输出 → 放弃，标记已处理
        _collect_comment_tasks(_src(), _binding(), _cfg(), _entry(), _res(),
                               it, "a/b issue#7", 60, "[ik]")
        assert "c1" in it.processed_comment_ids and it.comment_tasks == {}

    def test_collect_timeout_kills_process_group(self, tmp_path, monkeypatch):
        import issue_keeper.keeper as K
        monkeypatch.setattr(K, "_COMMENT_PROC_DIR", tmp_path)
        (tmp_path / "a-b-7.out").write_text("", encoding="utf-8")
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        it = types.SimpleNamespace(
            processed_comment_ids=set(), session_id=None,
            comment_tasks={"c1": {"pid": proc.pid, "started_at": time.time() - 9999,
                                  "attempts": 2}})
        # attempts 已=2：超时击杀后直接放弃（无重试）
        _collect_comment_tasks(_src(), _binding(), _cfg(), _entry(), _res(),
                               it, "a/b issue#7", 60, "[ik]")
        time.sleep(0.2)
        assert proc.poll() is not None, "超时任务应被进程组击杀"
        assert "c1" in it.processed_comment_ids


class TestCapAndSerial:
    def test_global_cap_blocks_dispatch(self, tmp_path, monkeypatch):
        state = _state_with_tasks(3)
        assert _count_comment_tasks(state) == 3
        comments = [_comment("c9")]
        dispatched = []
        monkeypatch.setattr("issue_keeper.keeper._spawn_comment_agent",
                            lambda *a, **k: dispatched.append(a) or 1)
        handled = _process_resource(
            src=_src(comments), binding=_binding(), config=_cfg(comment_max_in_flight=3),
            screener=_screener(), entry=_entry(), rs=_rs(), res=_res(),
            me="ik", timeout=60, visible_prefix="[ik]", state=state)
        assert handled == 0 and dispatched == []

    def test_same_issue_serial_no_second_dispatch(self, tmp_path, monkeypatch):
        import issue_keeper.keeper as K
        # 评论 c1 已在途 → c2 本轮不派发（同 issue 串行）
        comments = [_comment("c1"), _comment("c2")]
        rs = _rs()
        rs.item("issue:7").comment_tasks = {
            "c1": {"pid": 999999, "started_at": time.time(), "attempts": 1}}
        dispatched = []
        monkeypatch.setattr(K, "_spawn_comment_agent",
                            lambda *a, **k: dispatched.append(a) or 1)
        handled = _process_resource(
            src=_src(comments), binding=_binding(), config=_cfg(),
            screener=_screener(), entry=_entry(), rs=rs, res=_res(),
            me="ik", timeout=60, visible_prefix="[ik]", state=_state_with_tasks(0))
        assert dispatched == [] and handled == 0

    def test_restart_takes_over_by_pid(self):
        """daemon 重启后句柄丢失：活 pid 接管（不重复派发）、死 pid 收尸。"""
        import issue_keeper.keeper as K
        assert K._comment_proc_alive(1, None) is True    # pid=1 恒存在（launchd）
        assert K._comment_proc_alive(999999, None) is False
