"""pipeline_bridge RESULT key 归一化——issue #1「未发出回评」误报根因的回归测试。

业务早退路径返回 `posted`，成功路径返回 `comment_posted`，keeper 兜底判定只看
后者 → 所有早退终态曾被误报「管线异常终止」。normalize_result 在 bridge 出口补齐别名。
"""
import importlib.util
import os
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
BRIDGE = HERE.parent / "flows" / "pipeline_bridge.py"


def _load_bridge():
    """加载 bridge 模块；加载期间临时放宽 sys.path（bridge 顶层 import 仓外 plaita），
    加载完即恢复——防 plaita 树里的同名包（如 tests）污染本仓测试导入。"""
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("pipeline_bridge", BRIDGE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = saved


try:
    bridge = _load_bridge()
except Exception:  # 仓外依赖 plaita 不在时跳过（归一化逻辑本身无外部依赖）
    pytest.skip("pipeline_bridge 依赖仓外 plaita，跳过", allow_module_level=True)


def test_early_exit_posted_normalized_to_comment_posted():
    out = bridge.normalize_result({"status": "blocked", "posted": True})
    assert out["comment_posted"] is True


def test_posted_false_stays_false():
    out = bridge.normalize_result({"status": "guarded", "posted": False})
    assert out["comment_posted"] is False


def test_success_path_comment_posted_untouched():
    out = bridge.normalize_result({"status": "done", "comment_posted": True, "pushed": True})
    assert out["comment_posted"] is True
    assert "posted" not in out


def test_engine_error_without_posted_defaults_false():
    out = bridge.normalize_result({"status": "engine_error", "error": "boom"})
    assert out["comment_posted"] is False


# ── code 节点沙箱预算（#41 deliver / #45 merge 假失败的回归）──────────
# flow 的 deliver/merge 要跑 git ls-remote/commit/push，plaita 的 subprocess
# 沙箱默认只给 10s，包装层被墙钟杀掉但 push 已经落地 → 假失败。

def test_sandbox_timeout_default_is_not_the_10s_killer(monkeypatch):
    monkeypatch.delenv("PLAITA_SANDBOX_TIMEOUT", raising=False)
    bridge.ensure_sandbox_timeout()
    assert int(os.environ["PLAITA_SANDBOX_TIMEOUT"]) >= 300


def test_sandbox_timeout_empty_string_is_replaced(monkeypatch):
    """空串会让 plaita import 期的 int('') 抛 ValueError，必须补上。"""
    monkeypatch.setenv("PLAITA_SANDBOX_TIMEOUT", "")
    bridge.ensure_sandbox_timeout()
    assert os.environ["PLAITA_SANDBOX_TIMEOUT"] == "900"


def test_sandbox_timeout_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("PLAITA_SANDBOX_TIMEOUT", "120")
    bridge.ensure_sandbox_timeout()
    assert os.environ["PLAITA_SANDBOX_TIMEOUT"] == "120"


# ── 共享 cargo target（2026-09-28：#19/#30/#40 的 worktree 各自冷编译）──────────

def test_shared_cargo_target_points_at_main_clone(monkeypatch, tmp_path):
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    (tmp_path / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    got = bridge.ensure_shared_cargo_target({"main_clone": str(tmp_path)})
    assert got == str(tmp_path / "target")
    assert os.environ["CARGO_TARGET_DIR"] == str(tmp_path / "target")
    # 助手直接写 os.environ，monkeypatch 撤不掉 → 显式收回，别漏给后面的测试
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)


def test_shared_cargo_target_respects_explicit_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CARGO_TARGET_DIR", "/tmp/explicit-target")
    (tmp_path / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    assert bridge.ensure_shared_cargo_target({"main_clone": str(tmp_path)}) == ""
    assert os.environ["CARGO_TARGET_DIR"] == "/tmp/explicit-target"


def test_shared_cargo_target_skips_non_rust_repos(monkeypatch, tmp_path):
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    assert bridge.ensure_shared_cargo_target({"main_clone": str(tmp_path)}) == ""
    assert "CARGO_TARGET_DIR" not in os.environ


def test_shared_cargo_target_skips_missing_main_clone(monkeypatch):
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    assert bridge.ensure_shared_cargo_target({}) == ""
    assert bridge.ensure_shared_cargo_target({"main_clone": ""}) == ""


# ── PATH 补齐（2026-09-28：launchd 的 keeper PATH 没有 ~/.cargo/bin，gate 找不到 cargo）──

def test_tool_path_adds_cargo_bin_when_missing(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    added = bridge.ensure_tool_path()
    assert "cargo" in added
    assert os.environ["PATH"].split(os.pathsep)[0].endswith(".cargo/bin")


def test_tool_path_is_idempotent(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    bridge.ensure_tool_path()
    before = os.environ["PATH"]
    assert bridge.ensure_tool_path() == ""
    assert os.environ["PATH"] == before
