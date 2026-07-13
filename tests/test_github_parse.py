"""GitHub source 评论解析的纯逻辑测试（不打网络）。

覆盖 issue-level 评论与 PR 行内 review comment 的投影、排序。
"""

from issue_keeper.sources.github import (
    _issue_comment_to_comment,
    _review_comment_to_comment,
    _sort_comments,
)
from issue_keeper.sources import Comment


class TestIssueCommentParse:
    def test_rest_shape(self):
        c = _issue_comment_to_comment({
            "id": 11, "html_url": "https://github.com/o/r/issues/1#issuecomment-11",
            "user": {"login": "alice"}, "body": "hi", "created_at": "2026-07-10T00:00:00Z",
        })
        assert c.id == "11"
        assert c.author == "alice"
        assert c.body == "hi"
        assert c.url.endswith("#issuecomment-11")

    def test_gh_cli_shape(self):
        c = _issue_comment_to_comment({
            "id": 12, "url": "u", "author": {"login": "bob"},
            "body": "yo", "createdAt": "2026-07-10T00:00:01Z",
        }, gh_cli=True)
        assert c.id == "12" and c.author == "bob" and c.created_at.endswith("01Z")


class TestReviewCommentParse:
    def test_includes_location_prefix(self):
        c = _review_comment_to_comment({
            "id": 21, "html_url": "u", "user": {"login": "carol"},
            "body": "这里有 bug", "path": "src/a.py", "line": 42,
            "created_at": "2026-07-10T00:00:02Z",
        })
        assert c.id == "21"
        assert c.body.startswith("📍 src/a.py:42\n")
        assert "这里有 bug" in c.body

    def test_falls_back_to_original_line(self):
        c = _review_comment_to_comment({
            "id": 22, "user": {"login": "x"}, "body": "b",
            "path": "b.py", "line": None, "original_line": 7,
            "created_at": "2026-07-10T00:00:03Z",
        })
        assert "📍 b.py:7" in c.body

    def test_no_path_no_prefix(self):
        c = _review_comment_to_comment({
            "id": 23, "user": {"login": "x"}, "body": "b", "path": None,
            "created_at": "2026-07-10T00:00:04Z",
        })
        assert c.body == "b"


class TestSortComments:
    def test_chronological_by_created_at(self):
        cs = [
            Comment(id="3", url="", author="", body="", created_at="2026-07-10T00:00:09Z"),
            Comment(id="1", url="", author="", body="", created_at="2026-07-10T00:00:01Z"),
            Comment(id="2", url="", author="", body="", created_at="2026-07-10T00:00:05Z"),
        ]
        out = _sort_comments(cs)
        assert [c.id for c in out] == ["1", "2", "3"]

    def test_handles_missing_created_at(self):
        cs = [
            Comment(id="9", url="", author="", body="", created_at=""),
            Comment(id="1", url="", author="", body="", created_at="2026-07-10T00:00:01Z"),
        ]
        out = _sort_comments(cs)
        # 空时间排在前
        assert out[0].id == "9"
