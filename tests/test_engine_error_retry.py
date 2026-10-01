"""engine_error 自动重试的计数契约（2026-10-01 off-by-one 回归）。

背景：reaper 的自动重试分支上线当日 0 次触发、24 次直接升级——根因是
尾部连续数已含本次终态行（bridge _finish / reaper 代记都先写台账再计数），
而决策处又 +1，首败即 n_err=2。回归契约：trailing 计数**含本次**，
决策阈值 `n_err < 2` 直接用该值（首败 1 → 重试，二连 2 → 升级）。
"""
import json
from pathlib import Path

import pytest

from issue_keeper.keeper import _consecutive_engine_errors


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    # _consecutive_engine_errors 走 Path("~/.issue-keeper/...").expanduser()
    # ——expanduser 读 os.environ["HOME"]，改 HOME 即隔离
    monkeypatch.setenv("HOME", str(tmp_path))
    ledger_file = tmp_path / ".issue-keeper" / "pipeline" / "runs.jsonl"
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    yield ledger_file


def _append(repo, issue, status):
    import os
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+0800")   # 必须新鲜：连击有 12h 窗
    with open(os.environ["HOME"] + "/.issue-keeper/pipeline/runs.jsonl", "a") as f:
        f.write(json.dumps({"repo": repo, "issue": issue, "status": status,
                            "ts": ts}) + "\n")


def test_首败含本次_trailing_1应重试(ledger):
    _append("jeffkit/recursive", 77, "engine_error")
    # 决策处不再 +1：trailing=1 < 2 → 自动重试
    assert _consecutive_engine_errors("jeffkit/recursive", 77) == 1


def test_二连_trailing_2应升级(ledger):
    _append("jeffkit/recursive", 78, "engine_error")
    _append("jeffkit/recursive", 78, "engine_error")
    assert _consecutive_engine_errors("jeffkit/recursive", 78) == 2


def test_非engine_error打断连击(ledger):
    _append("jeffkit/recursive", 79, "engine_error")
    _append("jeffkit/recursive", 79, None)          # dispatch 行 status=None
    _append("jeffkit/recursive", 79, "engine_error")
    assert _consecutive_engine_errors("jeffkit/recursive", 79) == 1


def test_他issue与他仓不串账(ledger):
    _append("jeffkit/recursive", 80, "engine_error")
    _append("jeffkit/recursive", 81, "engine_error")
    _append("jeffkit/recursive-providers", 80, "engine_error")
    assert _consecutive_engine_errors("jeffkit/recursive", 80) == 1
    assert _consecutive_engine_errors("jeffkit/recursive", 82) == 0


def test_12h窗口外的陈旧失败不进连击(ledger):
    """连击跨天不衰减的语义修复：窗口外（>12h）记录出局，首败重新获得重试。"""
    from datetime import datetime, timedelta
    old_ts = (datetime.now() - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%S+0800")
    new_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+0800")
    with open(Path.home() / ".issue-keeper" / "pipeline" / "runs.jsonl", "a") as f:
        f.write(json.dumps({"repo": "jeffkit/recursive", "issue": 90,
                            "ts": old_ts, "status": "engine_error"}) + "\n")
        f.write(json.dumps({"repo": "jeffkit/recursive", "issue": 90,
                            "ts": new_ts, "status": "engine_error"}) + "\n")
    # 窗口内只有 1 条 → 首败重试（旧实现会把 20h 前的那条也算进去=2 升级）
    assert _consecutive_engine_errors("jeffkit/recursive", 90) == 1
