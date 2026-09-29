"""回评礼仪化测试：确定性消毒 + LLM 改写降级逻辑（全部离线，不真调 LLM）。"""

import pytest

from issue_keeper.config import load_config
from issue_keeper.reply import (
    ReplyPolishConfig, _strip_code_fence, polish, sanitize,
)
from tests.test_config import _db_path, _seed_project, _valid_screener


class TestSanitize:
    def test_redacts_mac_path(self):
        assert sanitize("看 /Users/kong/projects/x/src/a.rs 的实现") == \
            "看 [REDACTED-PATH] 的实现"

    def test_redacts_linux_path(self):
        assert sanitize("log at /home/kk/x.log") == "log at [REDACTED-PATH]"

    def test_redacts_secret_assignment(self):
        assert sanitize("api_key: sk-123") == "[REDACTED-SECRET]"
        assert sanitize("TOKEN=abc") == "[REDACTED-SECRET]"

    def test_keeps_repo_relative_refs(self):
        text = "改动在 `src/tools/transport.rs:50` 与 `crates/cli`，见 #12"
        assert sanitize(text) == text

    def test_plain_text_untouched(self):
        assert sanitize("普通结论，lib 2264 测试全绿。") == "普通结论，lib 2264 测试全绿。"


class TestStripFence:
    def test_strips_markdown_fence(self):
        assert _strip_code_fence("```markdown\n正文\n```") == "正文"

    def test_strips_bare_fence(self):
        assert _strip_code_fence("```\n正文\n```") == "正文"

    def test_keeps_unfenced(self):
        assert _strip_code_fence("正文保持原样") == "正文保持原样"


def _cfg(**kw) -> ReplyPolishConfig:
    base = dict(enabled=True, provider="openai", api_key="sk-x",
                base_url="https://llm.example/v1", model="m-1")
    base.update(kw)
    return ReplyPolishConfig(**base)


_RAW = (
    "好的，我现在先调查一下这个问题。首先我看了 src/a.rs，然后我改了它，"
    "然后我又跑了测试，现在我将总结。\n\n"
    "修复完成：a.rs 的空指针已修，commit abc1234。改动文件 src/a.rs，"
    "测试 12 个全绿。工作目录 /Users/kong/projects/x 已验证。\n\n"
    "修复完成：a.rs 的空指针已修，commit abc1234。改动文件 src/a.rs，"
    "测试 12 个全绿。工作目录 /Users/kong/projects/x 已验证。"
)


class TestPolish:
    def test_disabled_returns_sanitized_original(self):
        out = polish(_RAW, _cfg(enabled=False))
        assert "/Users/kong" not in out
        assert "[REDACTED-PATH]" in out
        assert "我现在先调查" in out  # 不改写，过程叙述保留

    def test_missing_creds_falls_back_to_sanitized(self):
        out = polish(_RAW, _cfg(api_key=None))
        assert "[REDACTED-PATH]" in out
        assert "我现在先调查" in out

    def test_short_text_skips_rewrite(self):
        short = "修好了，commit abc1234。"
        assert polish(short, _cfg()) == short

    def test_empty_text_untouched(self):
        assert polish("", _cfg()) == ""

    def test_llm_failure_falls_back_to_sanitized(self, monkeypatch):
        import issue_keeper.reply as mod

        def _boom(cfg, prompt):
            raise OSError("network down")

        monkeypatch.setattr(mod, "_call_llm", _boom)
        out = polish(_RAW, _cfg())
        assert "[REDACTED-PATH]" in out
        assert "我现在先调查" in out

    def test_success_returns_rewritten_and_resanitized(self, monkeypatch):
        import issue_keeper.reply as mod

        def _fake(cfg, prompt):
            # 改写结果里故意复述了一个路径——必须被二次消毒
            # （\S+ 会连尾部标点一起吃掉，与 flow 出害口消毒行为一致）
            return "```markdown\n修复空指针，commit abc1234，见 /Users/kong/secret/a.rs 好了\n```"

        monkeypatch.setattr(mod, "_call_llm", _fake)
        out = polish(_RAW, _cfg())
        assert out == "修复空指针，commit abc1234，见 [REDACTED-PATH] 好了"

    def test_empty_llm_output_falls_back(self, monkeypatch):
        import issue_keeper.reply as mod
        monkeypatch.setattr(mod, "_call_llm", lambda cfg, prompt: "  ")
        out = polish(_RAW, _cfg())
        assert "[REDACTED-PATH]" in out

    def test_long_text_truncated_for_llm(self, monkeypatch):
        import issue_keeper.reply as mod
        seen = {}

        def _fake(cfg, prompt):
            seen["prompt"] = prompt
            return "结论：ok"

        monkeypatch.setattr(mod, "_call_llm", _fake)
        long_raw = ("过程叙述。" * 4000)  # > 默认 max_chars
        out = polish(long_raw, _cfg(max_chars=4000))
        assert len(seen["prompt"]) <= 4000 + len("\n…[原文过长已截断]")
        assert out == "结论：ok"


class TestConfigWiring:
    def test_reply_polish_inherits_screener_creds(self, tmp_path):
        _seed_project(tmp_path, repo="p1")
        p = tmp_path / "config.yaml"
        p.write_text(
            "state_file: " + str(tmp_path / "state.json") + "\n"
            "internal_db: " + _db_path(tmp_path) + "\n"
            + _valid_screener(),
            encoding="utf-8",
        )
        cfg = load_config(p)
        rp = cfg.reply_polish
        assert rp.enabled is True
        assert rp.api_key == "sk-test"
        assert rp.base_url == "https://api.deepseek.com/v1"
        assert rp.model == "deepseek-chat"

    def test_reply_polish_explicit_overrides(self, tmp_path):
        _seed_project(tmp_path, repo="p1")
        p = tmp_path / "config.yaml"
        p.write_text(
            "state_file: " + str(tmp_path / "state.json") + "\n"
            "internal_db: " + _db_path(tmp_path) + "\n"
            + _valid_screener()
            + "reply_polish:\n"
              "  enabled: false\n"
              "  provider: anthropic\n"
              "  model: glm-5.3-flash\n"
              "  max_chars: 8000\n",
            encoding="utf-8",
        )
        cfg = load_config(p)
        rp = cfg.reply_polish
        assert rp.enabled is False
        assert rp.provider == "anthropic"
        assert rp.model == "glm-5.3-flash"  # 显式覆盖
        assert rp.api_key == "sk-test"      # 未显式的仍继承
        assert rp.max_chars == 8000

    def test_reply_polish_bad_provider_raises(self, tmp_path):
        _seed_project(tmp_path, repo="p1")
        p = tmp_path / "config.yaml"
        p.write_text(
            "state_file: " + str(tmp_path / "state.json") + "\n"
            "internal_db: " + _db_path(tmp_path) + "\n"
            + _valid_screener()
            + "reply_polish:\n"
              "  provider: gemini\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="reply_polish.provider"):
            load_config(p)


def test_sanitize_rewrites_unexpanded_command_substitution():
    """#19 实证：LLM 把想执行的命令原样写进回评（`$(git rev-parse …)` 上屏），
    GitHub 不做命令替换，读者只看到假哈希。消毒改写为如实说明。"""
    t = "已合入 main（`$(git rev-parse --short origin/main)`，rebase 后 ff 推入）。"
    out = sanitize(t)
    assert "$(" not in out
    assert "git rev-parse --short origin/main" in out
    assert "未在发布时执行" in out
    # 其余内容不受影响
    assert "rebase 后 ff 推入" in out
