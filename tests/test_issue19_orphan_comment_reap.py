"""#19 孤儿评论任务：closed 资源的 comment_tasks 永远没人收（收尸只见 open），
3 条陈尸占满 comment_max_in_flight=3 → 全仓评论停摆。修复=两层：
(a) 轮末 drop_orphan_comment_tasks 把不在 open 集里的记录清掉；
(b) 闸门计数 _count_comment_tasks 只计存活 pid——账本口径≠容量口径，
    死记录不删（属主收尸可能还要读输出发回评）但也不占容量。
"""
from __future__ import annotations

import time
import types

from issue_keeper.config import Config, RepoBinding, ScreenerConfig
from issue_keeper.state import State, drop_orphan_comment_tasks
from issue_keeper.keeper import (
    _count_comment_tasks,
    _process_resource,
)
from issue_keeper.sources import Comment, Resource


def _cfg(**kw) -> Config:
    return Config(pipeline_mode=False, opt_out_labels=[], **kw)


def _res(number=7, status="inbox") -> Resource:
    return Resource(kind="issue", number=number, title="t", body="", state="open",
                    labels=[], author="alice", created_at="", updated_at="",
                    status=status, actor_type="human", source_ref="")


def _comment(cid="c9", author="okguitar") -> Comment:
    return Comment(id=cid, url="", author=author, body="请回评", created_at="")


def _binding() -> RepoBinding:
    return RepoBinding(repo="a/b", profile="p", agent_label="alpha-agent")


def _src(comments=None):
    return types.SimpleNamespace(
        move_status=lambda *a, **k: (True, "x"),
        list_comments=lambda *a, **k: comments or [],
        web_url=lambda *a, **k: "http://x/1",
    )


def _rs():
    class _RS:
        def __init__(self):
            self._items = {}
        def item(self, key):
            if key not in self._items:
                self._items[key] = types.SimpleNamespace(
                    in_flight_since=None, processed=True, blocked=False,
                    processed_comment_ids=set(), session_id=None, comment_tasks={})
            return self._items[key]
    return _RS()


def _screener():
    return ScreenerConfig(enabled=False, provider="openai", api_key=None,
                          base_url=None, model=None, on_unsafe="skip", max_chars=8000)


def _entry():
    return types.SimpleNamespace(name="fake", is_hub=False, cwd=None, env={},
                                 timeout_secs=0)


def _zombie_task(started_days_ago=4.0):
    """陈尸任务：pid 不存在（99999999）+ started_at 远古。"""
    return {"pid": 99999999, "started_at": time.time() - started_days_ago * 86400,
            "attempts": 1}


class TestOrphanReap:
    def test_closed_item_tasks_dropped(self):
        """验收 1：processed item 不在 open 集 → 清扫后 comment_tasks 清空、计数归零。"""
        state = State()
        rs = state.repo("b")
        it = rs.item("2")
        it.processed = True
        it.comment_tasks = {"IC_dead": dict(_zombie_task())}
        n = drop_orphan_comment_tasks(state, open_keys=set())
        assert n == 1
        assert it.comment_tasks == {}
        assert _count_comment_tasks(state) == 0

    def test_open_item_tasks_kept(self):
        """open 资源的在途任务不受轮末清扫影响（交给正常收尸）。"""
        state = State()
        it = state.repo("b").item("7")
        it.comment_tasks = {"c1": {"pid": 1, "started_at": time.time(), "attempts": 1}}
        n = drop_orphan_comment_tasks(state, open_keys={"b:7"})
        assert n == 0
        assert "c1" in it.comment_tasks

    def test_list_failure_repo_whole_slug_skipped(self):
        """列表失败的仓（skip_slugs）整仓跳过——「不在 open 集」≠「已关闭」，
        可能只是这轮 gh/网络抖动没列出来；此时删记录等于删活任务的收尸凭据，
        属主同轮也被同一故障挡住，回评就永远发不出了（审查 NEEDS_FIX 项）。"""
        state = State()
        dead = state.repo("ok").item("2")      # 真 closed（仓列表成功、不在 open 集）
        dead.comment_tasks = {"IC_dead": dict(_zombie_task())}
        live = state.repo("flaky").item("9")   # 仓列表失败：pid 活着、输出未收
        live.comment_tasks = {"c1": {"pid": 1, "started_at": time.time(), "attempts": 1}}

        n = drop_orphan_comment_tasks(state, open_keys=set(),
                                      skip_slugs={"flaky"})
        assert n == 1, "只清列表成功仓上的真孤儿"
        assert dead.comment_tasks == {}
        assert live.comment_tasks == {"c1": live.comment_tasks["c1"]}, (
            "列表失败仓的在途任务记录必须原样保留")

    def test_list_failure_param_defaults_to_no_skip(self):
        """不传 skip_slugs 时行为不变（向后兼容，扫描内全部照清）。"""
        state = State()
        it = state.repo("b").item("2")
        it.comment_tasks = {"IC_dead": dict(_zombie_task())}
        assert drop_orphan_comment_tasks(state, open_keys=set()) == 1
        assert it.comment_tasks == {}

    def test_zombie_records_dont_count_toward_cap(self):
        """账本口径=容量口径：3 条死 pid 陈尸不计数（修复前恒返 3 占满闸）。"""
        state = types.SimpleNamespace(repos={
            f"r{i}": types.SimpleNamespace(items={
                f"issue:{i}": types.SimpleNamespace(comment_tasks={
                    "IC_dead": dict(_zombie_task())})})
            for i in range(3)})
        assert _count_comment_tasks(state) == 0

    def test_new_comment_dispatches_despite_three_zombies(self):
        """验收 2：state 预置 3 条「死 pid + 已关闭 issue」陈尸 + 新评论 → 仍派发。"""
        state = types.SimpleNamespace(repos={
            f"r{i}": types.SimpleNamespace(items={
                f"issue:{i}": types.SimpleNamespace(comment_tasks={
                    "IC_dead": dict(_zombie_task())})})
            for i in range(3)})
        assert _count_comment_tasks(state) == 0, "死记录不应占容量"
        dispatched = []
        import issue_keeper.keeper as K
        orig_spawn = K._spawn_comment_agent
        K._spawn_comment_agent = lambda *a, **k: dispatched.append(a) or 424242
        rs = _rs()
        try:
            handled = _process_resource(
                src=_src([_comment("c9")]), binding=_binding(),
                config=_cfg(comment_max_in_flight=3),
                screener=_screener(), entry=_entry(), rs=rs, res=_res(),
                me="ik", timeout=60, visible_prefix="[ik]", state=state)
        finally:
            K._spawn_comment_agent = orig_spawn
        assert dispatched and handled == 1, "闸门未满，新评论必须照常派发"
        # 派发登记落在处理中资源（issue#7）的 item 上
        assert "c9" in rs.item("7").comment_tasks

    def test_alive_task_counts(self):
        """活进程：正常计数（真实容量占用）。"""
        state = types.SimpleNamespace(repos={"r": types.SimpleNamespace(items={
            "issue:1": types.SimpleNamespace(comment_tasks={
                "c1": {"pid": 1, "started_at": time.time(), "attempts": 1}})})})
        assert _count_comment_tasks(state) == 1


class TestRunOnceListFailureKeepsRecords:
    """run_once 端到端回归（审查 NEEDS_FIX 项）：绑定的 list_open 抛异常时，
    该仓的 comment_tasks 记录——包括 pid 活着、输出还没收的——必须原样保留，
    等列表恢复后的下一轮由属主正常收尸发布。"""

    def test_failed_repo_records_survive_round(self, tmp_path, monkeypatch):
        import issue_keeper.keeper as K
        from issue_keeper.config import Config, RepoBinding
        from issue_keeper.state import load_state, save_state

        binding = RepoBinding(repo="a/b", profile="p")
        cfg = Config(pipeline_mode=False, opt_out_labels=[], repos=[binding],
                     state_file=tmp_path / "state.json")

        def _boom(*a, **k):
            raise RuntimeError("gh 网络抖动")
        monkeypatch.setattr(K, "process_repo", lambda *a, **k: 0)
        monkeypatch.setattr(K, "keeper_patrol", lambda *a, **k: 0)
        monkeypatch.setattr(K, "_ensure_source", _boom)

        state = State()
        live = state.repo("a-b").item("7")
        live.comment_tasks = {"c1": {"pid": 1, "started_at": time.time(),
                                     "attempts": 1}}
        dead = state.repo("a-b").item("2")
        dead.comment_tasks = {"IC_dead": dict(_zombie_task())}
        save_state(cfg.state_path, state)

        assert K.run_once(cfg) == 0

        back = load_state(cfg.state_path).repo("a-b")
        assert back.item("7").comment_tasks, (
            "列表失败仓的在途记录被轮末清扫删掉 → 回评永远发不出（NEEDS_FIX 场景）")
        assert back.item("2").comment_tasks == {"IC_dead": dead.comment_tasks["IC_dead"]}, (
            "列表失败的仓整仓跳过：真孤儿也留到列表恢复的下轮再清")

    def test_healthy_repo_still_swept(self, tmp_path, monkeypatch):
        """对照：列表成功的仓，closed 上的真孤儿照常清。"""
        import issue_keeper.keeper as K
        from issue_keeper.config import Config, RepoBinding
        from issue_keeper.state import load_state, save_state
        from issue_keeper.sources import Resource

        binding = RepoBinding(repo="a/b", profile="p")
        cfg = Config(pipeline_mode=False, opt_out_labels=[], repos=[binding],
                     state_file=tmp_path / "state.json")

        class _Src:
            def list_open(self, repo, kinds, labels=None):
                return [Resource(kind="issue", number=9, title="t", body="",
                                 state="open", labels=[], author="alice",
                                 created_at="", updated_at="")]

        monkeypatch.setattr(K, "process_repo", lambda *a, **k: 0)
        monkeypatch.setattr(K, "keeper_patrol", lambda *a, **k: 0)
        monkeypatch.setattr(K, "_ensure_source", lambda *a, **k: _Src())

        state = State()
        state.repo("a-b").item("2").comment_tasks = {
            "IC_dead": dict(_zombie_task())}
        save_state(cfg.state_path, state)

        K.run_once(cfg)

        assert load_state(cfg.state_path).repo("a-b").item("2").comment_tasks == {}
