"""keeper 的防循环判定与 review 自动通过逻辑测试（纯逻辑，不调 agent）。"""

from issue_keeper.config import Config, RepoBinding, KeeperPatrolConfig
from issue_keeper.keeper import (
    _is_bot_output, _should_auto_review, _agent_label, _visible_prefix, _preamble,
    keeper_patrol, _find_keeper_binding, _patrol_candidates,
)
from issue_keeper.sources import Resource


def _res(*, status="inbox", author="alice", actor_type="human", number=1) -> Resource:
    return Resource(
        kind="issue", number=number, title="t", body="", state="open",
        labels=[], author=author, created_at="", updated_at="",
        status=status, actor_type=actor_type,
    )


class _FakeSrc:
    """只满足 _supports_status 检查（有 move_status 方法）。"""

    def move_status(self, *a, **kw):  # noqa: D401
        return True, "x"


class TestIsBotOutput:
    def test_marker_hits(self):
        assert _is_bot_output("<!-- issue-keeper-bot -->\n正文", "<!-- issue-keeper-bot -->", "[issue-keeper:x]") is True

    def test_visible_prefix_hits(self):
        assert _is_bot_output("[issue-keeper:alpha]\n正文", "<!-- issue-keeper-bot -->", "[issue-keeper:alpha]") is True

    def test_clean_text_misses(self):
        assert _is_bot_output("普通的 bug 报告", "<!-- issue-keeper-bot -->", "[issue-keeper:alpha]") is False

    def test_empty_body_misses(self):
        assert _is_bot_output("", "<!-- issue-keeper-bot -->", "[issue-keeper:alpha]") is False


class TestVisiblePrefix:
    def test_prefix_uses_agent_label(self):
        b = RepoBinding(repo="a/b", profile="p", agent_label="alpha-agent")
        c = Config()
        assert _agent_label(b, c) == "alpha-agent"
        assert _visible_prefix(b, c) == "[issue-keeper:alpha-agent]"

    def test_falls_back_to_agent_from_user(self):
        b = RepoBinding(repo="a/b", profile="p")
        c = Config(agent_from_user="ik")
        assert _agent_label(b, c) == "ik"


class TestPreambleRole:
    def _cfg(self):
        # 至少一个项目让 _roster 不空
        return Config(repos=[RepoBinding(repo="self", profile="p", agent_label="me-agent")])

    def test_keeper_role_uses_keeper_preamble(self):
        b = RepoBinding(repo="self", profile="p", agent_label="me-agent", role="keeper")
        text = _preamble(b, self._cfg(), "me-agent")
        assert "keeper" in text
        assert "HitL" in text or "hitl" in text
        assert "send_and_wait_reply" in text
        # 仍含花名册
        assert "可用项目" in text

    def test_agent_role_uses_agent_preamble(self):
        b = RepoBinding(repo="self", profile="p", agent_label="me-agent", role="agent")
        text = _preamble(b, self._cfg(), "me-agent")
        # 普通 agent 提示词不含 keeper 管理向专属内容
        assert "send_and_wait_reply" not in text
        assert "管理向" not in text
        assert "可用项目" in text


class TestShouldAutoReview:
    def _cfg(self, default_review_agent=""):
        return Config(default_review_agent=default_review_agent)

    def test_agent_self_review(self):
        b = RepoBinding(repo="a/b", profile="p", agent_label="alpha-agent")
        res = _res(status="review", author="alpha-agent", actor_type="agent")
        should, actor, atype = _should_auto_review(_FakeSrc(), b, self._cfg(), res)
        assert should is True
        assert actor == "alpha-agent"
        assert atype == "agent"

    def test_human_issue_with_matching_review_agent(self):
        b = RepoBinding(repo="a/b", profile="p", agent_label="reviewer")
        res = _res(status="review", author="alice", actor_type="human")
        should, actor, atype = _should_auto_review(_FakeSrc(), b, self._cfg("reviewer"), res)
        assert should is True

    def test_human_issue_without_review_agent_waits(self):
        b = RepoBinding(repo="a/b", profile="p", agent_label="alpha")
        res = _res(status="review", author="alice", actor_type="human")
        should, _, _ = _should_auto_review(_FakeSrc(), b, self._cfg(""), res)
        assert should is False

    def test_non_review_status_no_auto_review(self):
        b = RepoBinding(repo="a/b", profile="p", agent_label="alpha-agent")
        res = _res(status="doing", author="alpha-agent", actor_type="agent")
        should, _, _ = _should_auto_review(_FakeSrc(), b, self._cfg(), res)
        assert should is False

    def test_review_agent_mismatch_waits(self):
        # 当前 agent 不是配置的 review_agent → 不自动 review
        b = RepoBinding(repo="a/b", profile="p", agent_label="alpha")
        res = _res(status="review", author="alice", actor_type="human")
        should, _, _ = _should_auto_review(_FakeSrc(), b, self._cfg("reviewer"), res)
        assert should is False


class TestKeeperPatrol:
    """keeper 巡检：代人类 review 等人处理的 issue。用真实 internal db + monkeypatch invoke_agent。"""

    def _setup(self, tmp_path, monkeypatch):
        from issue_keeper.sources.internal import InternalSource
        from issue_keeper.state import State
        from issue_keeper.profile import AgentReply
        from issue_keeper.screener import ScreenerConfig
        import issue_keeper.keeper as kmod

        db = str(tmp_path / "internal.db")
        keeper_b = RepoBinding(
            repo="issue-keeper", profile="claude-code", source="internal",
            agent_label="issue-keeper-agent", cwd=str(tmp_path),
            internal_db=db, role="keeper",
        )
        proj_b = RepoBinding(
            repo="proj-a", profile="claude-code", source="internal",
            agent_label="a-agent", cwd=str(tmp_path),
            internal_db=db, role="agent",
        )
        cfg = Config(
            screener=ScreenerConfig(
                enabled=False, provider="openai", api_key=None,
                base_url=None, model=None, on_unsafe="skip", max_chars=8000,
            ),
            repos=[proj_b, keeper_b],
            human_label="kongjie",
            keeper_patrol=KeeperPatrolConfig(enabled=True, interval_cycles=1, stale_inbox_secs=0),
        )

        # 在 proj-a 建一条人提 issue，推到 review（等人接手）
        src = InternalSource(RepoBinding(repo="proj-a", profile="", source="internal",
                                         agent_label="setup", internal_db=db))
        res = src.create_issue("proj-a", "issue", "登录 bug", "点登录无响应", "alice",
                               actor_type="human")
        src.move_status("proj-a", res, "review", actor="alice", actor_type="human",
                        comment="agent 已回复，待人 review")

        captured = {}
        def fake_invoke(entry, message, session_id, *, from_user, default_timeout):
            captured["message"] = message
            captured["session_id"] = session_id
            return AgentReply(session_id="patrol-sess", text="已代人类 review 通过，move 到 done。")
        monkeypatch.setattr(kmod, "invoke_agent", fake_invoke)

        state = State()
        return cfg, state, src, db, captured

    def test_patrol_reviews_human_review_issue(self, tmp_path, monkeypatch):
        cfg, state, src, db, captured = self._setup(tmp_path, monkeypatch)
        handled = keeper_patrol(cfg, state, {}, {})
        assert handled == 1
        # 调了 keeper，消息含代人类 review 语境
        assert "代人类 review" in captured["message"]
        assert "kongjie" in captured["message"]
        # keeper 以自己身份发了评论
        comments = src.list_comments("proj-a", src.get_issue("proj-a", "issue", 1))
        assert any(c.author == "issue-keeper-agent" for c in comments)
        # state 记了快照
        key = state.patrol_key("proj-a", "issue", 1)
        assert key in state.patrol

    def test_patrol_skips_when_no_new_activity(self, tmp_path, monkeypatch):
        cfg, state, src, db, captured = self._setup(tmp_path, monkeypatch)
        keeper_patrol(cfg, state, {}, {})  # 第一轮处理
        # 第二轮：无新活动，不应再调 keeper
        captured.clear()
        handled = keeper_patrol(cfg, state, {}, {})
        assert handled == 0
        assert "message" not in captured

    def test_patrol_disabled(self, tmp_path, monkeypatch):
        cfg, state, src, db, captured = self._setup(tmp_path, monkeypatch)
        cfg.keeper_patrol.enabled = False
        assert keeper_patrol(cfg, state, {}, {}) == 0
        assert "message" not in captured

    def test_patrol_no_keeper_binding(self, tmp_path, monkeypatch):
        cfg, state, src, db, captured = self._setup(tmp_path, monkeypatch)
        # 把 keeper 绑定改成普通 agent
        for b in cfg.repos:
            b.role = "agent"
        assert keeper_patrol(cfg, state, {}, {}) == 0

    def test_patrol_ignores_agent_filed_issues(self, tmp_path, monkeypatch):
        from issue_keeper.sources.internal import InternalSource
        from issue_keeper.config import RepoBinding as RB
        cfg, state, src, db, captured = self._setup(tmp_path, monkeypatch)
        # 再加一条 agent 提的 review issue（应被忽略，agent 自 review）
        src.create_issue("proj-a", "issue", "agent todo", "x", "a-agent", actor_type="agent")
        # 把它推到 review
        r2 = src.get_issue("proj-a", "issue", 2)
        src.move_status("proj-a", r2, "review", actor="a-agent", actor_type="agent")
        handled = keeper_patrol(cfg, state, {}, {})
        # 只处理人提的那一条（#1）
        assert handled == 1
        assert "登录 bug" in captured["message"]
        assert "agent todo" not in captured["message"]

    def test_find_keeper_binding(self):
        assert _find_keeper_binding(Config(repos=[
            RepoBinding(repo="a", profile="p", role="agent"),
            RepoBinding(repo="b", profile="p", role="keeper"),
        ])).repo == "b"
        assert _find_keeper_binding(Config(repos=[
            RepoBinding(repo="a", profile="p", role="agent"),
        ])) is None
