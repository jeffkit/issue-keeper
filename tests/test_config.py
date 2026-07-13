"""配置加载与校验测试。"""

import os
from pathlib import Path

import pytest

from issue_keeper.config import load_config


def _write(tmp: Path, body: str) -> Path:
    p = tmp / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _valid_screener():
    return (
        "screener:\n"
        "  enabled: true\n"
        "  provider: openai\n"
        "  api_key: sk-test\n"
        '  base_url: "https://api.deepseek.com/v1"\n'
        "  model: deepseek-chat\n"
    )


class TestScreenerFailSafe:
    def test_missing_screener_section_raises(self, tmp_path):
        cfg = _write(tmp_path, "repos:\n  - repo: a/b\n    profile: deepseek\n")
        with pytest.raises(ValueError, match="screener"):
            load_config(cfg)

    def test_enabled_not_declared_raises(self, tmp_path):
        cfg = _write(tmp_path, "screener:\n  provider: openai\n")
        with pytest.raises(ValueError, match="enabled"):
            load_config(cfg)

    def test_enabled_true_missing_creds_raises(self, tmp_path):
        cfg = _write(tmp_path, "screener:\n  enabled: true\n  provider: openai\n")
        with pytest.raises(ValueError, match="缺少"):
            load_config(cfg)

    def test_invalid_provider_raises(self, tmp_path):
        cfg = _write(tmp_path, "screener:\n  enabled: false\n  provider: gemini\n")
        with pytest.raises(ValueError, match="provider"):
            load_config(cfg)

    def test_invalid_on_unsafe_raises(self, tmp_path):
        cfg = _write(tmp_path, "screener:\n  enabled: false\n  on_unsafe: delete\n")
        with pytest.raises(ValueError, match="on_unsafe"):
            load_config(cfg)

    def test_enabled_false_allowed_without_creds(self, tmp_path):
        # 明确放行：不需要凭据
        cfg = _write(
            tmp_path,
            "repos:\n  - repo: a/b\n    profile: deepseek\n" + "screener:\n  enabled: false\n",
        )
        c = load_config(cfg)
        assert c.screener.enabled is False


class TestConfigLoading:
    def test_valid_config(self, tmp_path):
        cfg = _write(
            tmp_path,
            "poll_interval_secs: 42\n"
            "default_review_agent: reviewer\n"
            + _valid_screener() +
            "repos:\n"
            "  - repo: owner/repo\n"
            "    profile: deepseek\n"
            "    source: github_token\n"
            "    agent_label: proj-a-agent\n"
            "    cwd: ~/projects/proj-a\n",
        )
        c = load_config(cfg)
        assert c.poll_interval_secs == 42
        assert c.default_review_agent == "reviewer"
        assert len(c.repos) == 1
        b = c.repos[0]
        assert b.repo == "owner/repo"
        assert b.source == "github_token"
        assert b.agent_label == "proj-a-agent"
        assert b.repo_slug == "owner-repo"
        assert c.screener.enabled is True
        assert c.screener.api_key == "sk-test"

    def test_env_expansion_in_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GH_TEST_TOKEN", "tok-123")
        cfg = _write(
            tmp_path,
            _valid_screener() +
            "repos:\n"
            "  - repo: a/b\n"
            "    profile: deepseek\n"
            "    github_token: ${GH_TEST_TOKEN}\n",
        )
        c = load_config(cfg)
        assert c.repos[0].github_token == "tok-123"

    def test_empty_repos_rejected_by_runner(self, tmp_path):
        # load_config 不拒绝空 repos（由 _run_keeper 拒），这里只确认不抛
        cfg = _write(tmp_path, _valid_screener() + "repos: []\n")
        c = load_config(cfg)
        assert c.repos == []

    def test_nonpositive_interval_raises(self, tmp_path):
        cfg = _write(
            tmp_path,
            "poll_interval_secs: 0\n" + _valid_screener() + "repos: []\n",
        )
        with pytest.raises(ValueError, match="poll_interval_secs"):
            load_config(cfg)
