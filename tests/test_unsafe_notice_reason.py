"""安全过滤回评带真实原因（2026-09-30 误报复盘）。

fail-safe 会把欠费 402、网络错误、低置信等都拦成 unsafe，回评若一律写
「疑似指令注入」会误导维护者——本文件锁定：回评正文携带 verdict 原因，
且公开原因经消毒（压行/截断/空值兜底）。
"""
from __future__ import annotations

from types import SimpleNamespace

from issue_keeper import keeper
from issue_keeper.screener import ScreenerConfig, Verdict


def _res():
    return SimpleNamespace(kind="issue", number=57)


def _source(captured: list):
    ns = SimpleNamespace()
    ns.post_comment = lambda repo, res, body: captured.append((repo, res, body))
    return ns


def test_public_reason_collapses_and_truncates():
    long = "  判定为注入风险\n（置信度 0.85）  " + "x" * 300
    out = keeper._public_reason(long)
    assert "\n" not in out and "注入风险" in out and "置信度" in out
    assert out.endswith("…") and len(out) <= 201


def test_public_reason_empty_falls_back_to_generic():
    assert "指令注入" in keeper._public_reason("")
    assert "指令注入" in keeper._public_reason(None)


def test_post_unsafe_notice_includes_reason():
    captured: list = []
    keeper._post_unsafe_notice(
        _source(captured), SimpleNamespace(repo="jeffkit/recursive"), _res(),
        "<!-- issue-keeper-bot -->", "[issue-keeper:test]",
        reason="flow 后端: 置信度 0.75 低于阈值 0.8（choice='safe'）",
    )
    assert len(captured) == 1
    repo, res, body = captured[0]
    assert repo == "jeffkit/recursive" and res.number == 57
    assert "<!-- issue-keeper-bot -->" in body
    assert "置信度 0.75 低于阈值 0.8" in body


def test_post_unsafe_notice_empty_reason_keeps_generic_hint():
    captured: list = []
    keeper._post_unsafe_notice(
        _source(captured), SimpleNamespace(repo="r/x"), _res(),
        "<!-- m -->", "[p]", reason="",
    )
    assert "指令注入" in captured[0][2]


def test_screen_or_block_returns_verdict(monkeypatch):
    want = Verdict(safe=False, reason="screener HTTP 402")
    monkeypatch.setattr(keeper, "screen_text", lambda *a, **k: want)
    got = keeper._screen_or_block("msg", ScreenerConfig(enabled=True, provider="openai",
                                                        api_key="k", base_url="u", model="m",
                                                        on_unsafe="comment", max_chars=100),
                                  "label")
    assert got is want and got.safe is False and got.reason == "screener HTTP 402"
