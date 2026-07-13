"""screener 的纯逻辑测试（不发真实 HTTP）。"""

from issue_keeper.screener import (
    _anthropic_messages_url,
    _extract_json,
    _openai_chat_url,
    _truncate,
    screen,
    ScreenerConfig,
    Verdict,
)


class TestExtractJson:
    def test_pure_json(self):
        assert _extract_json('{"safe": true, "reason": "ok"}') == {"safe": True, "reason": "ok"}

    def test_json_with_surrounding_text(self):
        text = '好的，判定结果：{"safe": false, "reason": "含指令"} 以上。'
        out = _extract_json(text)
        assert out == {"safe": False, "reason": "含指令"}

    def test_no_json_returns_none(self):
        assert _extract_json("纯文本没有 JSON") is None

    def test_broken_json_returns_none(self):
        assert _extract_json("{not valid json}") is None

    def test_non_dict_json_returns_none(self):
        # 数组不是 dict，应被拒绝
        assert _extract_json("[1, 2, 3]") is None


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("abc", 100) == "abc"

    def test_long_text_truncated(self):
        out = _truncate("x" * 100, 10)
        assert out.startswith("x" * 10)
        assert "已截断" in out


class TestScreenFailSafe:
    """配置不全时按不安全处理（fail-safe），绝不打网络。"""

    def test_missing_creds_is_unsafe(self):
        cfg = ScreenerConfig(
            enabled=True, provider="openai",
            api_key=None, base_url=None, model=None,
            on_unsafe="skip", max_chars=8000,
        )
        v = screen("hello", cfg, source_label="t")
        assert v.safe is False
        assert "未配置" in v.reason


class TestEndpointUrls:
    def test_anthropic_without_v1(self):
        assert _anthropic_messages_url("https://api.anthropic.com") == "https://api.anthropic.com/v1/messages"

    def test_anthropic_with_v1_no_double(self):
        assert _anthropic_messages_url("https://glm.example.com/v1") == "https://glm.example.com/v1/messages"

    def test_anthropic_trailing_slash(self):
        assert _anthropic_messages_url("https://x.com/v1/") == "https://x.com/v1/messages"

    def test_openai_chat_url(self):
        assert _openai_chat_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1/chat/completions"
