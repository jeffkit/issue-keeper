"""issue #1 — pipeline 终态 comment_posted 键名归一化（原 [wip] 转正 + 扩充）。

历史缺陷：8 个早退出害口返回的键是 `posted`，只有 done 终态返回 `comment_posted`；
keeper 兜底（keeper.py `pres.get("comment_posted")`）只认后者，导致所有早退终态
（如 blocked）即使已发出业务回评，也会再被追加一条「管线异常终止」误报评论。
真实样本：recursive #31 / #32（status=blocked，回评已发，仍收到异常终止提示）。

验收：`posted` / `comment_posted` 归一化后，任意早退终态为 True 时不追加误报评论。
"""

import inspect
import pathlib
import re

FLOW_SRC = pathlib.Path(__file__).resolve().parent.parent / "flows" / "issue_pipeline_flow.py"
BRIDGE_SRC = pathlib.Path(__file__).resolve().parent.parent / "flows" / "pipeline_bridge.py"
KEEPER_SRC = pathlib.Path(__file__).resolve().parent.parent / "issue_keeper" / "keeper.py"


def _exit_keys():
    """从 flow 源码提取所有 return dict 中与回评发布相关的键名。"""
    src = FLOW_SRC.read_text(encoding="utf-8")
    return re.findall(r'"status": "(\w+)",[^}]*?"(posted|comment_posted)"', src)


class TestEarlyExitPostedKey:
    def test_all_exits_use_comment_posted(self):
        keys = _exit_keys()
        assert keys, "未能在 flow 源码中找到终态 return"
        bad = [(s, k) for s, k in keys if k != "comment_posted"]
        assert not bad, f"终态仍在用非归一化键名: {bad}"

    def test_expected_terminal_statuses_covered(self):
        statuses = {s for s, _ in _exit_keys()}
        expected = {"rejected", "blocked", "invalid", "onhold", "nochange",
                    "abort", "partial", "guarded", "basemismatch", "done"}
        assert expected <= statuses, f"缺少终态: {expected - statuses}"

    def test_done_uses_comment_posted(self):
        src = FLOW_SRC.read_text(encoding="utf-8")
        assert '"comment_posted": post.posted' in src


class TestKeeperFallbackNormalization:
    def test_invoke_pipeline_normalizes_posted(self):
        """_invoke_pipeline 应把 RESULT 中 posted 键归一化为 comment_posted。"""
        from issue_keeper.keeper import _invoke_pipeline

        src = inspect.getsource(_invoke_pipeline)
        assert 'pres.get("comment_posted", pres.get("posted"))' in src, (
            "_invoke_pipeline 未对早退 posted 键做归一化——早退终态的已发回评会被误判为未发"
        )


class TestBridgeResultPropagation:
    def test_bridge_normalizes_early_exit_posted(self):
        """bridge 台账也应归一 posted，否则旧 JSON 产物下 runs.jsonl 的 comment_posted 恒为 None。"""
        src = BRIDGE_SRC.read_text(encoding="utf-8")
        assert 'result.get("comment_posted", ' not in src  # 不允许只读不兜底
        assert 'result.get("posted")' in src
