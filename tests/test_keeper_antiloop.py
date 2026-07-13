"""keeper 的防循环判定与 review 自动通过逻辑测试（纯逻辑，不调 agent）。"""

from issue_keeper.config import Config, RepoBinding
from issue_keeper.keeper import _is_bot_output, _should_auto_review, _agent_label, _visible_prefix
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
