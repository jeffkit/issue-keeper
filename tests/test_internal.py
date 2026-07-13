"""InternalSource 状态机与持久化测试（用临时 SQLite，不碰默认库）。"""

from pathlib import Path

from issue_keeper.config import RepoBinding
from issue_keeper.sources.internal import (
    ACTOR_AGENT,
    ACTOR_HUMAN,
    ACTIVE_STATUSES,
    InternalSource,
)


def _src(tmp_path: Path, agent_label: str = "alpha-agent") -> InternalSource:
    binding = RepoBinding(
        repo="proj-test",
        profile="",
        source="internal",
        agent_label=agent_label,
        internal_db=str(tmp_path / "internal.db"),
    )
    return InternalSource(binding=binding)


class TestCreateIssue:
    def test_human_issue_starts_in_inbox(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "bug", "body", "alice", actor_type=ACTOR_HUMAN)
        assert r.status == "inbox"
        assert r.actor_type == "human"
        assert r.number == 1

    def test_agent_issue_starts_in_todo(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "auto", "body", "alpha-agent", actor_type=ACTOR_AGENT)
        assert r.status == "todo"
        assert r.actor_type == "agent"
        assert r.number == 1

    def test_number_increments_per_kind(self, tmp_path):
        src = _src(tmp_path)
        src.create_issue("proj-test", "issue", "a", "", "alice")
        src.create_issue("proj-test", "issue", "b", "", "alice")
        pr = src.create_issue("proj-test", "pr", "pr1", "", "alice")
        # issue 编号 1,2；PR 独立编号从 1 开始
        assert src.get_issue("proj-test", "issue", 1).number == 1
        assert src.get_issue("proj-test", "issue", 2).number == 2
        assert pr.number == 1 and pr.kind == "pr"

    def test_initial_history_recorded(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice")
        h = src.list_status_history("proj-test", r)
        assert len(h) == 1
        assert h[0]["from_status"] is None
        assert h[0]["to_status"] == "inbox"


class TestMoveStatus:
    def test_move_records_history_and_returns_from(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice")
        ok, from_status = src.move_status("proj-test", r, "doing", actor="alice", comment="开始")
        assert ok is True
        assert from_status == "inbox"
        h = src.list_status_history("proj-test", r)
        assert len(h) == 2
        assert h[1]["to_status"] == "doing"
        assert h[1]["comment"] == "开始"

    def test_move_to_same_status_is_noop(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice")  # inbox
        ok, from_status = src.move_status("proj-test", r, "inbox")
        assert ok is False
        assert from_status == "inbox"

    def test_move_assignee_on_doing(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice")
        src.move_status("proj-test", r, "doing", actor="alpha-agent", actor_type=ACTOR_AGENT)
        fresh = src.get_issue("proj-test", "issue", r.number)
        assert fresh.assignee == "alpha-agent"

    def test_move_review_sets_assignee_for_agent(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice")
        src.move_status("proj-test", r, "review", actor="alpha-agent", actor_type=ACTOR_AGENT)
        assert src.get_issue("proj-test", "issue", r.number).assignee == "alpha-agent"

    def test_done_clears_assignee(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice")
        src.move_status("proj-test", r, "doing", actor="alpha-agent", actor_type=ACTOR_AGENT)
        src.move_status("proj-test", r, "done", actor="alpha-agent", actor_type=ACTOR_AGENT)
        assert src.get_issue("proj-test", "issue", r.number).assignee == ""

    def test_invalid_status_raises(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice")
        try:
            src.move_status("proj-test", r, "bogus")
        except ValueError:
            return
        raise AssertionError("应拒绝非法 status")


class TestListOpen:
    def test_only_active_statuses_listed(self, tmp_path):
        src = _src(tmp_path)
        a = src.create_issue("proj-test", "issue", "a", "", "alice")  # inbox
        b = src.create_issue("proj-test", "issue", "b", "", "alice")  # inbox
        src.move_status("proj-test", b, "done")
        opened = src.list_open("proj-test", ["issue"])
        numbers = {r.number for r in opened}
        assert a.number in numbers
        assert b.number not in numbers  # done 不算开放

    def test_closed_not_listed(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice")
        src.close_issue("proj-test", r, actor="alice")
        assert r.number not in {x.number for x in src.list_open("proj-test", ["issue"])}


class TestComments:
    def test_post_comment_uses_agent_label_identity(self, tmp_path):
        src = _src(tmp_path, agent_label="alpha-agent")
        r = src.create_issue("proj-test", "issue", "a", "", "alice")
        src.post_comment("proj-test", r, "agent 的回复")
        cmts = src.list_comments("proj-test", r)
        assert len(cmts) == 1
        assert cmts[0].author == "alpha-agent"

    def test_add_comment_with_custom_author(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice")
        src.add_comment("proj-test", r, "我也遇到", "bob", actor_type=ACTOR_HUMAN)
        cmts = src.list_comments("proj-test", r)
        assert cmts[0].author == "bob"


class TestPersistence:
    def test_reopen_keeps_data(self, tmp_path):
        src = _src(tmp_path)
        src.create_issue("proj-test", "issue", "a", "", "alice")
        # 新实例指向同一个 db
        src2 = _src(tmp_path)
        assert len(src2.list_all("proj-test", ["issue"])) == 1


class TestLabels:
    def test_create_with_labels_persisted(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice", labels=["bug", "ai"])
        assert r.labels == ["bug", "ai"]
        fresh = src.get_issue("proj-test", "issue", r.number)
        assert fresh.labels == ["bug", "ai"]

    def test_create_without_labels_defaults_empty(self, tmp_path):
        src = _src(tmp_path)
        r = src.create_issue("proj-test", "issue", "a", "", "alice")
        assert r.labels == []

    def test_list_open_filters_by_label_intersection(self, tmp_path):
        src = _src(tmp_path)
        src.create_issue("proj-test", "issue", "a", "", "alice", labels=["bug"])
        src.create_issue("proj-test", "issue", "b", "", "alice", labels=["feature"])
        src.create_issue("proj-test", "issue", "c", "", "alice", labels=["bug", "ai"])
        src.create_issue("proj-test", "issue", "d", "", "alice")  # 无标签

        bug = src.list_open("proj-test", ["issue"], labels=["bug"])
        titles = {r.title for r in bug}
        assert titles == {"a", "c"}

        ai = src.list_open("proj-test", ["issue"], labels=["AI"])  # 大小写不敏感
        assert {r.title for r in ai} == {"c"}

        none_filter = src.list_open("proj-test", ["issue"], labels=None)
        assert len(none_filter) == 4

    def test_list_open_label_no_match_returns_empty(self, tmp_path):
        src = _src(tmp_path)
        src.create_issue("proj-test", "issue", "a", "", "alice", labels=["bug"])
        assert src.list_open("proj-test", ["issue"], labels=["nope"]) == []
