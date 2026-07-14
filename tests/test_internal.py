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


class TestProjectsTable:
    def test_upsert_and_list_meta(self, tmp_path):
        src = _src(tmp_path)
        src.upsert_project(name="proj-a", agent_label="a-agent", cwd="/x/a",
                           profile="claude-code", source="internal")
        metas = {m["name"]: m for m in src.list_projects_meta()}
        assert metas["proj-a"]["agent_label"] == "a-agent"
        assert metas["proj-a"]["cwd"] == "/x/a"
        assert metas["proj-a"]["intro"] == ""

    def test_upsert_preserves_intro_on_resync(self, tmp_path):
        src = _src(tmp_path)
        src.upsert_project(name="proj-a", agent_label="a-agent")
        assert src.set_project_intro("proj-a", "我是 a") is True
        # 重新 upsert（模拟 sync 重跑）不应覆盖 intro
        src.upsert_project(name="proj-a", agent_label="a-agent-v2", cwd="/y")
        m = {m["name"]: m for m in src.list_projects_meta()}["proj-a"]
        assert m["intro"] == "我是 a"
        assert m["agent_label"] == "a-agent-v2"  # 元数据更新了

    def test_set_intro_by_label(self, tmp_path):
        src = _src(tmp_path)
        src.upsert_project(name="proj-a", agent_label="a-agent")
        assert src.set_project_intro_by_label("a-agent", "hi") == "proj-a"
        assert src.set_project_intro_by_label("nope", "x") is None
        m = {m["name"]: m for m in src.list_projects_meta()}["proj-a"]
        assert m["intro"] == "hi"

    def test_counts_include_zero_issue_project(self, tmp_path):
        src = _src(tmp_path)
        src.upsert_project(name="empty", agent_label="e-agent")
        src.upsert_project(name="with-issues", agent_label="w-agent")
        src.create_issue("with-issues", "issue", "t", "", "alice")
        counts = {p["project"]: p for p in src.list_projects_with_counts()}
        assert counts["empty"]["total"] == 0 and counts["empty"]["open"] == 0
        assert counts["with-issues"]["total"] == 1 and counts["with-issues"]["open"] == 1
        assert counts["empty"]["agent_label"] == "e-agent"

    def test_counts_include_issues_only_project_not_in_table(self, tmp_path):
        """有 issue 但未 sync 进 projects 表的项目仍应出现（兼容旧行为）。"""
        src = _src(tmp_path)
        src.create_issue("ghost", "issue", "t", "", "alice")
        counts = {p["project"]: p for p in src.list_projects_with_counts()}
        assert "ghost" in counts
        assert counts["ghost"]["total"] == 1
        assert counts["ghost"]["agent_label"] == ""  # 无元数据

    def test_upsert_role_on_insert_and_default_agent(self, tmp_path):
        src = _src(tmp_path)
        src.upsert_project(name="proj-a", agent_label="a-agent", cwd="/x", role="keeper")
        m = {x["name"]: x for x in src.list_projects_meta()}["proj-a"]
        assert m["role"] == "keeper"
        # 不传 role 默认 agent
        src.upsert_project(name="proj-b", agent_label="b-agent", cwd="/y")
        m2 = {x["name"]: x for x in src.list_projects_meta()}["proj-b"]
        assert m2["role"] == "agent"

    def test_upsert_preserves_role_on_resync(self, tmp_path):
        """重新 upsert 不应把已标成 keeper 的项目降级回 agent。"""
        src = _src(tmp_path)
        src.upsert_project(name="proj-a", agent_label="a-agent", role="keeper")
        # 模拟 sync 重跑，不传 role（默认 agent）
        src.upsert_project(name="proj-a", agent_label="a-agent-v2", cwd="/y")
        m = {x["name"]: x for x in src.list_projects_meta()}["proj-a"]
        assert m["role"] == "keeper"
        assert m["agent_label"] == "a-agent-v2"

    def test_set_project_role_and_by_label(self, tmp_path):
        import pytest
        src = _src(tmp_path)
        src.upsert_project(name="proj-a", agent_label="a-agent")
        assert src.set_project_role("proj-a", "keeper") is True
        with pytest.raises(ValueError):
            src.set_project_role("proj-a", "bogus")  # 非法 role 抛错
        m = {x["name"]: x for x in src.list_projects_meta()}["proj-a"]
        assert m["role"] == "keeper"
        # by label
        assert src.set_project_role_by_label("a-agent", "agent") == "proj-a"
        assert src.set_project_role_by_label("nope", "agent") is None
        m2 = {x["name"]: x for x in src.list_projects_meta()}["proj-a"]
        assert m2["role"] == "agent"

    def test_counts_include_role(self, tmp_path):
        src = _src(tmp_path)
        src.upsert_project(name="k", agent_label="k-agent", role="keeper")
        counts = {p["project"]: p for p in src.list_projects_with_counts()}
        assert counts["k"]["role"] == "keeper"


class TestTeamDbOps:
    def _old_yaml(self, tmp_path, db_path) -> str:
        """旧版 config.yaml（带 repos 段），用于测试 import 迁移。"""
        p = tmp_path / "old_config.yaml"
        p.write_text(
            "screener:\n  enabled: false\n"
            f"internal_db: {db_path}\n"
            "repos:\n"
            "  - repo: proj-a\n"
            "    profile: claude-code\n"
            "    source: internal\n"
            "    agent_label: a-agent\n"
            "    cwd: /x/a\n"
            "    monitor_prs: true\n"
            "    env:\n"
            "      ANTHROPIC_API_KEY: ${DEEPSEEK_API_KEY}\n"
            "  - repo: proj-b\n"
            "    profile: claude-code\n"
            "    source: internal\n"
            "    agent_label: b-agent\n"
            "    cwd: /x/b\n",
            encoding="utf-8",
        )
        return str(p)

    def test_import_from_old_yaml(self, tmp_path):
        from issue_keeper.team import import_from_config, load_team
        db = str(tmp_path / "internal.db")
        members = import_from_config(self._old_yaml(tmp_path, db), db)
        m = {x.project: x for x in members}
        assert set(m) == {"proj-a", "proj-b"}
        assert m["proj-a"].agent_label == "a-agent"
        assert m["proj-a"].monitor_prs is True
        assert m["proj-b"].monitor_prs is False
        # env 入库（占位保留）
        from issue_keeper.sources.internal import InternalSource
        from issue_keeper.config import RepoBinding
        s = InternalSource(RepoBinding(repo="", profile="", source="internal",
                                       agent_label="x", internal_db=db))
        meta = {x["name"]: x for x in s.list_projects_meta()}
        assert meta["proj-a"]["env"] == {"ANTHROPIC_API_KEY": "${DEEPSEEK_API_KEY}"}

    def test_import_preserves_existing_intro(self, tmp_path):
        from issue_keeper.team import import_from_config, load_team, set_intro
        db = str(tmp_path / "internal.db")
        # 先 import 一次，设 intro，再 import 应保留
        import_from_config(self._old_yaml(tmp_path, db), db)
        set_intro("a-agent", "我是 a", db)
        import_from_config(self._old_yaml(tmp_path, db), db)
        m = {x.project: x for x in load_team(db)}
        assert m["proj-a"].intro == "我是 a"

    def test_import_migrates_legacy_teamjson(self, tmp_path, monkeypatch):
        import json
        from issue_keeper import team as team_mod
        from issue_keeper.team import import_from_config, load_team

        legacy = tmp_path / "team.json"
        monkeypatch.setattr(team_mod, "_LEGACY_TEAM_PATH", legacy)
        legacy.write_text(json.dumps({"members": [
            {"project": "proj-a", "agent_label": "a-agent", "cwd": "/x/a", "intro": "旧介绍 a"},
        ]}), encoding="utf-8")

        db = str(tmp_path / "internal.db")
        import_from_config(self._old_yaml(tmp_path, db), db)
        m = {x.project: x for x in load_team(db)}
        assert m["proj-a"].intro == "旧介绍 a"
        assert not legacy.exists()
        assert legacy.with_suffix(".json.migrated").exists()

    def test_add_and_remove_project(self, tmp_path):
        from issue_keeper.team import add_project_to_db, load_team, remove_project_from_db
        db = str(tmp_path / "internal.db")
        add_project_to_db(db, repo="proj-c", agent_label="c-agent", cwd="/x/c",
                          monitor_prs=True, env={"FOO": "bar"})
        m = {x.project: x for x in load_team(db)}
        assert m["proj-c"].monitor_prs is True
        assert remove_project_from_db(db, repo="proj-c") is True
        assert remove_project_from_db(db, repo="proj-c") is False  # 再删未命中
        assert "proj-c" not in {x.project for x in load_team(db)}
