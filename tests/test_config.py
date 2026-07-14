"""配置加载与校验测试。

项目绑定从 db 的 projects 表加载（单一源），所以测试要先 seed db 再 load_config。
screener / 全局旋钮 / agent_env 仍从 config.yaml 读。
"""

import os
from pathlib import Path

import pytest

from issue_keeper.config import load_config


def _db_path(tmp: Path) -> str:
    return str(tmp / "internal.db")


def _seed_project(tmp: Path, *, repo: str, agent_label: str = "x-agent",
                  cwd: str = "/x", profile: str = "claude-code",
                  source: str = "internal", monitor_prs: bool = False,
                  env: dict | None = None, intro: str = "") -> None:
    from issue_keeper.sources.internal import InternalSource
    from issue_keeper.config import RepoBinding
    b = RepoBinding(repo="", profile="", source="internal",
                    agent_label="seed", internal_db=_db_path(tmp))
    src = InternalSource(binding=b)
    src.upsert_project(name=repo, agent_label=agent_label, cwd=cwd,
                       profile=profile, source=source,
                       monitor_prs=monitor_prs, env=env)
    if intro:
        src.set_project_intro(repo, intro)


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


def _base(tmp: Path, extra: str = "") -> str:
    return (
        f"internal_db: {_db_path(tmp)}\n"
        + extra
    )


class TestScreenerFailSafe:
    def test_missing_screener_section_raises(self, tmp_path):
        cfg = _write(tmp_path, _base(tmp_path, "poll_interval_secs: 60\n"))
        with pytest.raises(ValueError, match="screener"):
            load_config(cfg)

    def test_enabled_not_declared_raises(self, tmp_path):
        cfg = _write(tmp_path, _base(tmp_path, "screener:\n  provider: openai\n"))
        with pytest.raises(ValueError, match="enabled"):
            load_config(cfg)

    def test_enabled_true_missing_creds_raises(self, tmp_path):
        cfg = _write(tmp_path, _base(tmp_path, "screener:\n  enabled: true\n  provider: openai\n"))
        with pytest.raises(ValueError, match="缺少"):
            load_config(cfg)

    def test_invalid_provider_raises(self, tmp_path):
        cfg = _write(tmp_path, _base(tmp_path, "screener:\n  enabled: false\n  provider: gemini\n"))
        with pytest.raises(ValueError, match="provider"):
            load_config(cfg)

    def test_invalid_on_unsafe_raises(self, tmp_path):
        cfg = _write(tmp_path, _base(tmp_path, "screener:\n  enabled: false\n  on_unsafe: delete\n"))
        with pytest.raises(ValueError, match="on_unsafe"):
            load_config(cfg)

    def test_enabled_false_allowed_without_creds(self, tmp_path):
        cfg = _write(tmp_path, _base(tmp_path, "screener:\n  enabled: false\n"))
        c = load_config(cfg)
        assert c.screener.enabled is False


class TestConfigLoading:
    def test_globals_and_repos_from_db(self, tmp_path):
        _seed_project(tmp_path, repo="owner/repo", agent_label="proj-a-agent",
                      cwd="~/projects/proj-a", profile="deepseek", source="github_cli")
        cfg = _write(
            tmp_path,
            _base(tmp_path,
                  "poll_interval_secs: 42\n"
                  "default_review_agent: reviewer\n"
                  + _valid_screener()
                  ),
        )
        c = load_config(cfg)
        assert c.poll_interval_secs == 42
        assert c.default_review_agent == "reviewer"
        assert len(c.repos) == 1
        b = c.repos[0]
        assert b.repo == "owner/repo"
        assert b.source == "github_cli"
        assert b.agent_label == "proj-a-agent"
        assert b.repo_slug == "owner-repo"
        assert c.screener.enabled is True
        assert c.screener.api_key == "sk-test"
        # internal_db 全局路径套到每个 binding
        assert b.internal_db == _db_path(tmp_path)

    def test_agent_env_template_applied_to_bindings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "key-xyz")
        _seed_project(tmp_path, repo="a")
        cfg = _write(
            tmp_path,
            _base(tmp_path,
                  _valid_screener() +
                  "agent_env:\n"
                  "  ANTHROPIC_API_KEY: ${DEEPSEEK_API_KEY}\n"
                  '  ANTHROPIC_BASE_URL: "https://api.deepseek.com/anthropic"\n',
                  ),
        )
        c = load_config(cfg)
        assert c.repos[0].env["ANTHROPIC_API_KEY"] == "key-xyz"
        assert c.repos[0].env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"

    def test_project_env_overrides_agent_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "global-key")
        _seed_project(tmp_path, repo="a",
                      env={"ANTHROPIC_API_KEY": "${DEEPSEEK_API_KEY}",
                           "EXTRA": "proj-only"})
        cfg = _write(
            tmp_path,
            _base(tmp_path,
                  _valid_screener() +
                  "agent_env:\n"
                  "  ANTHROPIC_API_KEY: ${DEEPSEEK_API_KEY}\n"
                  "  CLAUDE_MODEL: deepseek-chat\n",
                  ),
        )
        c = load_config(cfg)
        env = c.repos[0].env
        # 项目 env 的 ANTHROPIC_API_KEY 覆盖全局（都展开成同一值，但 EXTRA 来自项目）
        assert env["EXTRA"] == "proj-only"
        assert env["CLAUDE_MODEL"] == "deepseek-chat"  # 全局仍生效
        assert env["ANTHROPIC_API_KEY"] == "global-key"

    def test_monitor_prs_loaded_from_db(self, tmp_path):
        _seed_project(tmp_path, repo="a", monitor_prs=True)
        _seed_project(tmp_path, repo="b", monitor_prs=False)
        cfg = _write(tmp_path, _base(tmp_path, _valid_screener()))
        c = load_config(cfg)
        by_repo = {b.repo: b for b in c.repos}
        assert by_repo["a"].monitor_prs is True
        assert by_repo["b"].monitor_prs is False

    def test_empty_repos_when_db_empty(self, tmp_path):
        cfg = _write(tmp_path, _base(tmp_path, _valid_screener()))
        c = load_config(cfg)
        assert c.repos == []

    def test_nonpositive_interval_raises(self, tmp_path):
        cfg = _write(
            tmp_path,
            _base(tmp_path, "poll_interval_secs: 0\n" + _valid_screener()),
        )
        with pytest.raises(ValueError, match="poll_interval_secs"):
            load_config(cfg)
