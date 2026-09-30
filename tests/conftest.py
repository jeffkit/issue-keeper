"""issue-keeper 测试全局隔离。

metrics/workbench 的「台账兜底」默认读真实 ~/.issue-keeper/pipeline/runs.jsonl
（线上历史），测试不隔离就会被线上数据污染（例：summarize 多出 35 条 run）。
这里统一把两条兜底路径指到不存在的临时文件；需要台账的测试自行显式传
ledger_path。
"""

from __future__ import annotations

import pytest

from issue_keeper import metrics, workbench


@pytest.fixture(autouse=True)
def _isolate_run_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "LEDGER_PATH", tmp_path / "no-such-runs.jsonl")
    monkeypatch.setattr(workbench, "LEDGER_PATH", tmp_path / "no-such-runs.jsonl")
    yield
