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

    def test_failed_auto_retry_zero_is_honored(self, tmp_path):
        """#8：0 有意义（= 不自动重试），不得被 max(1, ...) 抬起。"""
        cfg = _write(tmp_path,
                     _base(tmp_path, "failed_auto_retry: 0\n" + _valid_screener()))
        assert load_config(cfg).failed_auto_retry == 0

    def test_failed_retry_and_needs_human_defaults(self, tmp_path):
        cfg = _write(tmp_path, _base(tmp_path, _valid_screener()))
        c = load_config(cfg)
        assert c.failed_auto_retry == 1
        assert c.pipeline_needs_human_label == "needs-human"

    def test_needs_human_label_override(self, tmp_path):
        cfg = _write(tmp_path, _base(
            tmp_path,
            'pipeline_needs_human_label: "等人处理"\n' + _valid_screener()))
        assert load_config(cfg).pipeline_needs_human_label == "等人处理"

    def test_console_queued_grace_secs_wired_from_yaml(self, tmp_path):
        """#18：宽限期必须真从 yaml 读——本仓已有两起「旋钮无人读取」的事故。"""
        cfg = _write(tmp_path, _base(
            tmp_path, "console_queued_grace_secs: 600\n" + _valid_screener()))
        assert load_config(cfg).console_queued_grace_secs == 600
        # 0 有意义（关掉宽限 = 退回旧自愈），不得被 max(1, ...) 抬起
        cfg0 = _write(tmp_path, _base(
            tmp_path, "console_queued_grace_secs: 0\n" + _valid_screener()))
        assert load_config(cfg0).console_queued_grace_secs == 0


class TestPipelineRepos:
    """v0.3 per-repo 管线契约解析。"""

    def _cfg(self, tmp_path, extra: str):
        cfg = _write(tmp_path, _base(tmp_path, _valid_screener() + extra))
        return load_config(cfg)

    def test_full_contract_parsed(self, tmp_path):
        c = self._cfg(tmp_path, (
            "pipeline_repos:\n"
            "  jeffkit/argusai:\n"
            "    base_branch: develop\n"
            "    setup_command: pnpm install --frozen-lockfile\n"
            "    test_command: pnpm test:run\n"
            "    push_mode: pr\n"
            "    review_mode: human\n"
            "    review_notes: 'schema 生成流程不可绕过'\n"
            "    timeout_overrides:\n"
            "      implement: 1200\n"
        ))
        pc = c.pipeline_repo_cfg("jeffkit/argusai")
        assert pc.enabled and pc.mode == "full"
        assert pc.base_branch == "develop"
        assert pc.setup_command == "pnpm install --frozen-lockfile"
        assert pc.test_command == "pnpm test:run"
        assert pc.resolved_push_mode("branch") == "pr"
        assert pc.resolved_review_mode("auto") == "human"
        assert pc.review_notes.startswith("schema")
        assert pc.timeout_overrides["implement"] == 1200
        assert pc.has_gate() is True
        # 空覆盖继承全局（resolved_* 的兜底语义）
        assert pc.resolved_push_mode("main") == "pr"

    def test_gates_parsed_with_paths_and_timeout(self, tmp_path):
        c = self._cfg(tmp_path, (
            "pipeline_repos:\n"
            "  jeffkit/recursive:\n"
            "    gates:\n"
            "      - name: fmt\n"
            "        command: cargo fmt --all --check\n"
            "        timeout_secs: 300\n"
            "      - name: tui-mutants\n"
            "        command: bash .dev/scripts/tui-mutants.sh\n"
            "        timeout_secs: 3600\n"
            "        paths:\n"
            "          - 'crates/recursive-tui/**'\n"
        ))
        pc = c.pipeline_repo_cfg("jeffkit/recursive")
        assert [g.name for g in pc.gates] == ["fmt", "tui-mutants"]
        assert pc.gates[1].paths == ["crates/recursive-tui/**"]
        # 整门预算 = gates 之和 + 300 缓冲
        assert pc.effective_gate_timeout() == 300 + 3600 + 300
        assert pc.has_gate() is True

    def test_legacy_test_commands_merged(self, tmp_path):
        c = self._cfg(tmp_path, (
            "pipeline_test_commands:\n"
            "  jeffkit/recursive: cargo test --workspace\n"
            "pipeline_repos:\n"
            "  jeffkit/argusai:\n"
            "    test_command: pnpm test:run\n"
        ))
        # 旧配置单独生效（未登记仓兜底）
        assert c.pipeline_repo_cfg("jeffkit/recursive").test_command == "cargo test --workspace"
        # 新契约优先于旧命令
        assert c.pipeline_repo_cfg("jeffkit/argusai").test_command == "pnpm test:run"
        # 登记了 gates 的仓不吃旧命令
        c2 = self._cfg(tmp_path, (
            "pipeline_test_commands:\n"
            "  jeffkit/recursive: cargo test --workspace\n"
            "pipeline_repos:\n"
            "  jeffkit/recursive:\n"
            "    gates:\n"
            "      - name: g\n"
            "        command: x\n"
        ))
        assert c2.pipeline_repo_cfg("jeffkit/recursive").test_command == ""
        assert c2.pipeline_repo_cfg("jeffkit/recursive").has_gate() is True

    def test_unregistered_repo_has_no_gate(self, tmp_path):
        c = self._cfg(tmp_path, "pipeline_repos:\n  jeffkit/x:\n    test_command: t\n")
        assert c.pipeline_repo_cfg("jeffkit/other").has_gate() is False

    def test_readonly_mode(self, tmp_path):
        c = self._cfg(tmp_path, (
            "pipeline_repos:\n"
            "  jeffkit/recursive-providers:\n"
            "    mode: readonly\n"
        ))
        pc = c.pipeline_repo_cfg("jeffkit/recursive-providers")
        assert pc.mode == "readonly" and pc.has_gate() is False  # readonly 不需要门

    def test_invalid_mode_raises(self, tmp_path):
        with pytest.raises(ValueError, match="mode"):
            self._cfg(tmp_path, "pipeline_repos:\n  a/b:\n    mode: aggressive\n")

    def test_invalid_push_mode_raises(self, tmp_path):
        with pytest.raises(ValueError, match="push_mode"):
            self._cfg(tmp_path, "pipeline_repos:\n  a/b:\n    push_mode: force\n")

    def test_gate_missing_name_raises(self, tmp_path):
        with pytest.raises(ValueError, match="gates"):
            self._cfg(tmp_path, "pipeline_repos:\n  a/b:\n    gates:\n      - command: x\n")

    def test_disabled_repo(self, tmp_path):
        c = self._cfg(tmp_path, (
            "pipeline_repos:\n"
            "  a/b:\n"
            "    enabled: false\n"
            "    test_command: t\n"
        ))
        assert c.pipeline_repo_cfg("a/b").enabled is False


class TestPipelineRepoLimits:
    """S5 按仓派发配额（2026-10-07）解析：空默认=不设限，行为不变。"""

    def _cfg(self, tmp_path, extra: str):
        cfg = _write(tmp_path, _base(tmp_path, _valid_screener() + extra))
        return load_config(cfg)

    def test_limits_parsed(self, tmp_path):
        c = self._cfg(tmp_path,
                      "pipeline_repo_limits:\n"
                      "  jeffkit/recursive: 2\n"
                      "  jeffkit/plaita: 0\n")
        assert c.pipeline_repo_limits == {"jeffkit/recursive": 2, "jeffkit/plaita": 0}

    def test_limits_default_empty(self, tmp_path):
        assert self._cfg(tmp_path, "").pipeline_repo_limits == {}

    def test_limits_bad_type_raises(self, tmp_path):
        with pytest.raises(ValueError, match="pipeline_repo_limits"):
            self._cfg(tmp_path, "pipeline_repo_limits: [a, b]\n")
