"""pipeline_bridge RESULT key 归一化——issue #1「未发出回评」误报根因的回归测试。

业务早退路径返回 `posted`，成功路径返回 `comment_posted`，keeper 兜底判定只看
后者 → 所有早退终态曾被误报「管线异常终止」。normalize_result 在 bridge 出口补齐别名。
"""
import importlib.util
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
